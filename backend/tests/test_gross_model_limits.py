"""Configuration-wide caps combine with model caps without implicit limits."""

from copy import deepcopy
import importlib
import json

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.api import compatibility
from backend.app.schemas.configuration import normalize_config
from backend.app.services.completion_service import CompletionService
from backend.app.services.connectors import ChatAPIError
from backend.app.services.model_limits import (
    InputContextLimitError,
    completion_options_with_limit,
    ensure_input_limit,
)
from backend.app.services.router import Router


completion_module = importlib.import_module("backend.app.services.completion_service")
limits_module = importlib.import_module("backend.app.services.model_limits")
router_module = importlib.import_module("backend.app.services.router")
client = TestClient(main.app)
MESSAGES = [{"role": "user", "content": "Hello"}]
TOOL = {"type": "function", "function": {
    "name": "lookup", "description": "Lookup an item",
    "parameters": {"type": "object", "properties": {}},
}}


def route(model="fixture", **limits):
    return {"provider": "mock", "model": model, "retries": 0, **limits}


def config(*routes, probability=False, **fields):
    sequences = ([{"type": "probability", "choices": [
        {**item, "probability": 1} for item in routes
    ]}] if probability else list(routes))
    return {"system_prompt": "", "past_memory": False, "sequences": sequences, **fields}


def reply():
    return {"message": {"role": "assistant", "content": "Done"}, "finish_reason": "stop"}


@pytest.mark.parametrize("field", ["gross_max_input_token", "gross_max_output_token"])
@pytest.mark.parametrize("value", [1, 100_000, 1_000_000_000])
def test_gross_caps_preserve_valid_values_without_changing_routes(field, value):
    supplied = config(route(), **{field: value})
    original = deepcopy(supplied)
    normalized = normalize_config(supplied)
    assert normalized[field] == value
    assert normalized["sequences"] == [route()]
    assert normalize_config(normalized) == normalized
    assert supplied == original


@pytest.mark.parametrize("field", ["gross_max_input_token", "gross_max_output_token"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2048", 1_000_000_001])
def test_invalid_gross_caps_are_rejected(field, value):
    with pytest.raises(ValueError, match=field):
        normalize_config(config(route(), **{field: value}))


@pytest.mark.parametrize("probability", [False, True])
def test_omitted_and_null_gross_and_model_caps_remain_unspecified(probability):
    legacy = normalize_config(config(route(), probability=probability))
    cleared = normalize_config(config(
        route(max_input_token=None, max_output_token=None,
              max_input_tokens=None, max_output_tokens=None), probability=probability,
        gross_max_input_token=None, gross_max_output_token=None,
    ))
    assert cleared == legacy
    assert "gross_max_input_token" not in cleared
    assert "gross_max_output_token" not in cleared
    assert "Infinity" not in json.dumps(cleared)


@pytest.mark.parametrize("direction", ["input", "output"])
@pytest.mark.parametrize("probability", [False, True])
@pytest.mark.parametrize("plural_value", [None, 4096])
def test_singular_model_cap_aliases_normalize_to_plural_fields(direction, probability, plural_value):
    singular, plural = f"max_{direction}_token", f"max_{direction}_tokens"
    supplied = config(route(**{singular: 4096, plural: plural_value}), probability=probability)
    original = deepcopy(supplied)
    normalized = normalize_config(supplied)
    item = normalized["sequences"][0]
    if probability:
        item = item["choices"][0]
    assert item[plural] == 4096
    assert singular not in item
    assert supplied == original


@pytest.mark.parametrize("direction", ["input", "output"])
@pytest.mark.parametrize("probability", [False, True])
def test_conflicting_singular_and_plural_model_caps_are_rejected(direction, probability):
    with pytest.raises(ValueError, match="Conflicting"):
        normalize_config(config(route(**{
            f"max_{direction}_token": 100, f"max_{direction}_tokens": 200,
        }), probability=probability))


@pytest.mark.parametrize("field", ["max_input_token", "max_output_token"])
@pytest.mark.parametrize("value", [0, True, "2048"])
def test_invalid_singular_model_caps_are_rejected(field, value):
    with pytest.raises(ValueError, match=field):
        normalize_config(config(route(**{field: value})))


def test_user_example_configuration_validates_without_adding_model_caps():
    supplied = {
        "context_window": 10, "memory_scope": "all_sessions", "memory_window": 20,
        "past_memory": True, "gross_max_input_token": 100000, "gross_max_output_token": 10000,
        "sequences": [{"model": "gpt-5-nano", "provider": "openai", "retries": 2, "effort": "minimal"}],
        "system_prompt": "You are a helpful, precise, and thoughtful AI assistant.",
    }
    response = client.post("/api/config/validate", json=supplied)
    assert response.status_code == 200, response.text
    normalized = normalize_config(supplied)
    assert normalized["gross_max_input_token"] == 100000
    assert normalized["gross_max_output_token"] == 10000
    assert normalized["sequences"] == supplied["sequences"]


@pytest.mark.parametrize("route_cap,gross_cap,expected", [
    (50, 100, 50), (100, 50, 50), (None, 50, 50), (50, None, 50),
])
def test_input_comparison_uses_only_the_smallest_supplied_cap(monkeypatch, route_cap, gross_cap, expected):
    monkeypatch.setattr(limits_module, "estimate_tokens", lambda _: 80)
    with pytest.raises(InputContextLimitError, match=f"input limit of {expected} "):
        ensure_input_limit(route(max_input_tokens=route_cap), MESSAGES,
                           config={"gross_max_input_token": gross_cap})


@pytest.mark.parametrize("null_caps", [False, True])
def test_unset_input_caps_skip_token_estimation_entirely(monkeypatch, null_caps):
    monkeypatch.setattr(limits_module, "estimate_tokens",
                        lambda _: pytest.fail("Unspecified input caps must skip estimation and comparison"))
    item = route(max_input_tokens=None, max_input_token=None) if null_caps else route()
    supplied = {"gross_max_input_token": None} if null_caps else {}
    ensure_input_limit(item, MESSAGES, system_prompt="instruction " * 1000,
                       options={"tools": [TOOL]}, config=supplied)


@pytest.mark.parametrize("route_cap,gross_cap,client_cap,expected", [
    (100, 80, 50, 50), (100, 80, 120, 80), (40, 80, 120, 40),
    (None, 80, None, 80), (40, None, None, 40), (None, None, 120, 120),
])
@pytest.mark.parametrize("key", ["max_tokens", "max_completion_tokens"])
def test_output_combines_model_gross_and_client_caps_without_increasing_request(
    route_cap, gross_cap, client_cap, expected, key,
):
    item = route(max_output_tokens=route_cap)
    supplied = {"gross_max_output_token": gross_cap}
    options = {key: client_cap} if client_cap is not None else {}
    original = deepcopy((item, supplied, options))
    result = completion_options_with_limit(item, options, config=supplied)
    expected_key = key if client_cap is not None else "max_completion_tokens"
    assert result == {expected_key: expected}
    assert (item, supplied, options) == original


def test_unset_output_caps_do_not_add_a_provider_option():
    options = {"temperature": 0.2}
    assert completion_options_with_limit(route(), options, config={}) == options
    assert completion_options_with_limit(route(max_output_tokens=None), options,
                                         config={"gross_max_output_token": None}) == options


@pytest.mark.parametrize("probability", [False, True])
@pytest.mark.parametrize("transport", ["completion", "chat"])
def test_gross_input_cap_blocks_every_route_without_retry_or_truncation(monkeypatch, probability, transport):
    supplied = config(route("first", retries=5, max_input_tokens=10000), route("fallback"),
                      probability=probability, gross_max_input_token=1)
    original = deepcopy((MESSAGES, supplied))
    if transport == "completion":
        service = CompletionService()
        monkeypatch.setattr(service, "_execute", lambda *args: pytest.fail("Oversized input reached a provider"))
        module = completion_module
        invoke = lambda: service.complete(MESSAGES, supplied, {})
    else:
        service = Router()
        monkeypatch.setattr(service, "_execute_single_provider",
                            lambda **kwargs: pytest.fail("Oversized input reached a provider"))
        module = router_module
        invoke = lambda: service.route_chat(MESSAGES, config=supplied)
    monkeypatch.setattr(module.random, "choices", lambda *args, **kwargs: [0])
    monkeypatch.setattr(module.time, "sleep", lambda _: pytest.fail("Oversized input must not retry"))
    with pytest.raises(InputContextLimitError):
        invoke()
    assert (MESSAGES, supplied) == original


@pytest.mark.parametrize("content_kind", ["system", "tool_schema", "tool_history", "response_schema"])
def test_gross_input_cap_includes_complete_system_and_tool_context(monkeypatch, content_kind):
    service = CompletionService()
    messages, options, fields = deepcopy(MESSAGES), {}, {"gross_max_input_token": 100}
    if content_kind == "system":
        fields["system_prompt"] = "instruction " * 1000
    elif content_kind == "tool_schema":
        options["tools"] = [{**TOOL, "function": {**TOOL["function"], "description": "schema " * 1000}}]
    elif content_kind == "tool_history":
        messages.extend([
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_one", "type": "function", "function": {
                    "name": "lookup", "arguments": '{"query":"' + "word " * 1000 + '"}',
                },
            }]},
            {"role": "tool", "tool_call_id": "call_one", "content": "result " * 1000},
        ])
    else:
        options["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "answer", "schema": {"type": "string", "description": "schema " * 1000},
        }}
    supplied = config(route(), **fields)
    original = deepcopy((messages, supplied, options))
    monkeypatch.setattr(service, "_execute", lambda *args: pytest.fail("Complete context must be checked first"))
    with pytest.raises(InputContextLimitError):
        service.complete(messages, supplied, options)
    assert (messages, supplied, options) == original


@pytest.mark.parametrize("probability", [False, True])
def test_gross_output_cap_is_reapplied_independently_on_completion_fallback(monkeypatch, probability):
    service, calls = CompletionService(), []
    supplied = config(route("offline", max_output_tokens=4), route("fallback"),
                      probability=probability, gross_max_output_token=80)
    options = {"max_tokens": 100}
    original = deepcopy((supplied, options))

    def execute(item, messages, bounded):
        calls.append((item["model"], deepcopy(messages), deepcopy(bounded)))
        if item["model"] == "offline":
            raise ChatAPIError("Offline")
        return reply()

    monkeypatch.setattr(service, "_execute", execute)
    monkeypatch.setattr(completion_module.random, "choices", lambda *args, **kwargs: [0])
    assert service.complete(MESSAGES, supplied, options)["model"] == "fallback"
    assert calls == [("offline", MESSAGES, {"max_tokens": 4}),
                     ("fallback", MESSAGES, {"max_tokens": 80})]
    assert (supplied, options) == original


def test_chat_fallback_uses_gross_output_cap_and_preserves_full_input(monkeypatch):
    service, calls = Router(), []
    prompt = "Complete system instruction"
    supplied = config(route("offline", max_output_tokens=4), route("fallback"),
                      gross_max_input_token=1000, gross_max_output_token=80)

    def execute(**kwargs):
        calls.append(deepcopy(kwargs))
        if kwargs["model"] == "offline":
            raise ChatAPIError("Offline")
        return "Done"

    monkeypatch.setattr(service, "_execute_single_provider", execute)
    assert service.route_chat(MESSAGES, system_prompt=prompt, config=supplied).model == "fallback"
    assert [(item["model"], item["max_output_tokens"]) for item in calls] == [("offline", 4), ("fallback", 80)]
    assert all(item["messages"] == MESSAGES and item["system_prompt"] == prompt for item in calls)


def test_gross_caps_round_trip_library_downloads_and_session_snapshots():
    supplied = config(route(max_input_token=4096, max_output_token=512),
                      gross_max_input_token=32000, gross_max_output_token=2048)
    created = client.post("/api/configs", json={"name": "Gross budgets", "model_id": "gross-budgets", "config": supplied})
    assert created.status_code == 201, created.text
    path = f"/api/configs/{created.json()['id']}"
    expected = normalize_config(supplied)
    assert client.get(path).json()["config"] == expected
    assert client.get(path + "/download").json() == expected
    session = client.post("/api/sessions", json={"config_id": created.json()["id"]})
    assert session.status_code == 201, session.text
    session_path = f"/api/sessions/{session.json()['session_id']}"
    assert client.get(session_path).json()["config"] == expected
    edited = deepcopy(expected)
    edited.update(gross_max_input_token=64000, gross_max_output_token=None)
    assert client.patch(session_path, json={"config": edited}).status_code == 200
    assert client.get(session_path).json()["config"] == normalize_config(edited)
    assert client.get(path).json()["config"] == expected
    assert client.patch(path, json={"config": edited}).status_code == 200
    assert client.get(path + "/download").json() == normalize_config(edited)


@pytest.mark.parametrize("database_free", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_gross_input_cap_returns_compatibility_context_error(monkeypatch, database_free, stream):
    monkeypatch.setattr(compatibility.settings, "completion_no_database_access", database_free)
    created = client.post("/api/configs", json={
        "name": "Small gross context", "model_id": "small-gross-context",
        "config": config(route(), gross_max_input_token=1),
    })
    assert created.status_code == 201, created.text
    monkeypatch.setattr(compatibility.completion_service, "_execute",
                        lambda *args: pytest.fail("Gross input cap must precede provider calls"))
    result = client.post("/v1/chat/completions", json={
        "model": "small-gross-context", "messages": MESSAGES, "stream": stream,
    })
    assert result.status_code == 400, result.text
    assert result.json()["error"]["code"] == "context_length_exceeded"
    assert result.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.parametrize("endpoint,prompt_field", [("/api/chat", "message"), ("/api/agent/run", "prompt")])
def test_small_gross_input_cap_returns_session_and_agent_client_error(monkeypatch, endpoint, prompt_field):
    supplied = config(route(), gross_max_input_token=1)
    created = client.post("/api/sessions", json={"config": supplied, "user_session": True})
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]
    monkeypatch.setattr(main.router, "_execute_single_provider",
                        lambda **kwargs: pytest.fail("Gross input cap must precede provider calls"))
    result = client.post(endpoint, json={"session_id": session_id, prompt_field: "Hello"})
    assert result.status_code == 400, result.text
    assert "exceeding the input limit" in result.json()["detail"]
    assert client.get(f"/api/sessions/{session_id}").json()["messages"] == []


def test_gross_output_cap_applies_to_each_agent_step_without_accumulating(monkeypatch):
    supplied = config(route(), gross_max_input_token=10000, gross_max_output_token=80)
    created = client.post("/api/sessions", json={"config": supplied, "user_session": True})
    assert created.status_code == 201, created.text
    calls = []

    def execute(**kwargs):
        calls.append(deepcopy(kwargs))
        if len(calls) == 1:
            return json.dumps({"action": {"tool": "gross_budget_lookup", "arguments": {}}, "final_answer": None})
        return json.dumps({"action": None, "final_answer": "Done"})

    main.agent_service.register_tool("gross_budget_lookup", "Lookup an item", {"type": "object"},
                                     handler=lambda _: "Observation from lookup")
    monkeypatch.setattr(main.router, "_execute_single_provider", execute)
    result = client.post("/api/agent/run", json={
        "session_id": created.json()["session_id"], "prompt": "Look it up", "max_steps": 2,
        "tools": ["gross_budget_lookup"],
    })
    assert result.status_code == 200, result.text
    assert result.json()["final_answer"] == "Done"
    assert [item["max_output_tokens"] for item in calls] == [80, 80]
    assert len(calls[1]["messages"]) > len(calls[0]["messages"])
    assert "Observation from lookup" in calls[1]["messages"][-1]["content"]


def test_gross_input_cap_rechecks_agent_context_after_tool_result(monkeypatch):
    supplied = config(route(), gross_max_input_token=1000, gross_max_output_token=80)
    created = client.post("/api/sessions", json={"config": supplied, "user_session": True})
    assert created.status_code == 201, created.text
    calls = []

    def execute(**kwargs):
        calls.append(deepcopy(kwargs))
        return json.dumps({"action": {"tool": "large_budget_lookup", "arguments": {}}, "final_answer": None})

    main.agent_service.register_tool("large_budget_lookup", "Lookup an item", {"type": "object"},
                                     handler=lambda _: "observation " * 10000)
    monkeypatch.setattr(main.router, "_execute_single_provider", execute)
    result = client.post("/api/agent/run", json={
        "session_id": created.json()["session_id"], "prompt": "Look it up", "max_steps": 2,
        "tools": ["large_budget_lookup"],
    })
    assert result.status_code == 400, result.text
    assert "exceeding the input limit" in result.json()["detail"]
    assert len(calls) == 1
