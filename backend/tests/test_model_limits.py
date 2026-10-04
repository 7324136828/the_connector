"""Per-model token budgets, routing fallback, and saved configuration contracts."""

from copy import deepcopy
import importlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.api import compatibility
from backend.app.schemas.configuration import normalize_config
from backend.app.services.completion_service import CompletionService
from backend.app.services.connectors import ChatAPIError
from backend.app.services.model_limits import InputContextLimitError, ensure_input_limit
from backend.app.services.router import Router


completion_module = importlib.import_module("backend.app.services.completion_service")
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
    if probability:
        sequences = [{"type": "probability", "choices": [
            {**item, "probability": 1} for item in routes
        ]}]
    else:
        sequences = list(routes)
    return {"system_prompt": "", "past_memory": False, "sequences": sequences, **fields}


def reply():
    return {"message": {"role": "assistant", "content": "Done"}, "finish_reason": "stop"}


@pytest.mark.parametrize("probability", [False, True])
def test_route_limits_normalize_preserve_boundaries_and_do_not_mutate(probability):
    supplied = config(route(max_input_tokens=1, max_output_tokens=1_000_000_000),
                      probability=probability)
    original = deepcopy(supplied)
    result = normalize_config(supplied)
    normalized_route = result["sequences"][0].get("choices", result["sequences"])[0]
    assert normalized_route["max_input_tokens"] == 1
    assert normalized_route["max_output_tokens"] == 1_000_000_000
    assert normalize_config(result) == result
    assert supplied == original


@pytest.mark.parametrize("field", ["max_input_tokens", "max_output_tokens"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2048", 1_000_000_001])
@pytest.mark.parametrize("probability", [False, True])
def test_invalid_route_limits_are_rejected(field, value, probability):
    with pytest.raises(ValueError, match=field):
        normalize_config(config(route(**{field: value}), probability=probability))


@pytest.mark.parametrize("probability", [False, True])
def test_omitted_and_null_limits_keep_legacy_configs_unlimited(probability):
    legacy = normalize_config(config(route(), probability=probability))
    cleared = normalize_config(config(route(max_input_tokens=None, max_output_tokens=None),
                                     probability=probability))
    assert cleared == legacy
    for step in legacy["sequences"]:
        for item in step.get("choices", [step]):
            assert "max_input_tokens" not in item and "max_output_tokens" not in item
    assert normalize_config(legacy) == legacy
    ensure_input_limit(route(), [{"role": "user", "content": "long " * 1000}])


@pytest.mark.parametrize("system_prompt,options", [
    ("instruction " * 1000, {}),
    ("", {"tools": [{**TOOL, "function": {**TOOL["function"], "description": "schema " * 1000}}]}),
    ("", {"response_format": {"type": "json_schema", "json_schema": {
        "name": "answer", "schema": {"type": "string", "description": "schema " * 1000},
    }}}),
    ("", {"tool_choice": {"type": "function", "function": {"name": "tool " * 1000}}}),
])
def test_input_budget_includes_system_prompt_and_model_visible_options(system_prompt, options):
    item = route(max_input_tokens=100)
    ensure_input_limit(item, MESSAGES)
    original = deepcopy((MESSAGES, options))
    with pytest.raises(InputContextLimitError):
        ensure_input_limit(item, MESSAGES, system_prompt=system_prompt, options=options)
    assert (MESSAGES, options) == original


def test_input_budget_counts_tool_call_arguments_and_results():
    messages = MESSAGES + [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_one", "type": "function", "function": {
                "name": "lookup", "arguments": '{"query":"' + "word " * 1000 + '"}',
            },
        }]},
        {"role": "tool", "tool_call_id": "call_one", "content": "result " * 1000},
    ]
    with pytest.raises(InputContextLimitError):
        ensure_input_limit(route(max_input_tokens=100), messages)


@pytest.mark.parametrize("probability", [False, True])
def test_completion_skips_small_input_route_without_retry_or_truncation(monkeypatch, probability):
    service, calls = CompletionService(), []
    monkeypatch.setattr(completion_module.random, "choices", lambda *args, **kwargs: [0])
    monkeypatch.setattr(completion_module.time, "sleep", lambda _: pytest.fail("Oversize inputs must not retry"))

    def execute(item, messages, options):
        calls.append((item["model"], deepcopy(messages), deepcopy(options)))
        return reply()

    monkeypatch.setattr(service, "_execute", execute)
    supplied = config(route("small", retries=5, max_input_tokens=1, max_output_tokens=2),
                      route("large", max_input_tokens=1000, max_output_tokens=80),
                      probability=probability)
    original = deepcopy(supplied)
    result = service.complete(MESSAGES, supplied, {})
    assert result["model"] == "large"
    assert calls == [("large", MESSAGES, {"max_completion_tokens": 80})]
    assert supplied == original


@pytest.mark.parametrize("probability", [False, True])
def test_all_small_completion_routes_raise_context_error_before_provider(monkeypatch, probability):
    service = CompletionService()
    monkeypatch.setattr(service, "_execute", lambda *args: pytest.fail("Provider must not receive oversized input"))
    monkeypatch.setattr(completion_module.time, "sleep", lambda _: pytest.fail("Oversize inputs must not retry"))
    with pytest.raises(InputContextLimitError) as error:
        service.complete(MESSAGES, config(route("one", retries=5, max_input_tokens=1),
                                         route("two", max_input_tokens=1), probability=probability), {})
    assert isinstance(error.value, ChatAPIError)


@pytest.mark.parametrize("key", ["max_tokens", "max_completion_tokens"])
@pytest.mark.parametrize("requested,expected", [(50, 50), (200, 100)])
def test_configured_output_cap_preserves_smaller_client_limit_and_key(monkeypatch, key, requested, expected):
    service, captured = CompletionService(), []
    monkeypatch.setattr(service, "_execute", lambda item, messages, options:
                        captured.append(deepcopy(options)) or reply())
    options = {key: requested}
    supplied = config(route(max_output_tokens=100))
    original = deepcopy((MESSAGES, supplied, options))
    service.complete(MESSAGES, supplied, options)
    assert captured == [{key: expected}]
    assert (MESSAGES, supplied, options) == original


@pytest.mark.parametrize("probability", [False, True])
@pytest.mark.parametrize("fallback_cap,client_options,expected", [
    (70, {"max_tokens": 100}, {"max_tokens": 70}),
    (None, {}, {}),
])
def test_completion_fallback_caps_are_independent(monkeypatch, probability, fallback_cap, client_options, expected):
    service, calls = CompletionService(), []
    monkeypatch.setattr(completion_module.random, "choices", lambda *args, **kwargs: [0])
    monkeypatch.setattr(completion_module.time, "sleep", lambda _: None)

    def execute(item, messages, options):
        calls.append((item["model"], deepcopy(options)))
        if item["model"] == "offline":
            raise ChatAPIError("Offline")
        return reply()

    monkeypatch.setattr(service, "_execute", execute)
    supplied = config(route("offline", retries=1, max_output_tokens=4),
                      route("fallback", max_output_tokens=fallback_cap), probability=probability)
    original = deepcopy((supplied, client_options))
    result = service.complete(MESSAGES, supplied, client_options)
    assert result["model"] == "fallback"
    cap_key = "max_tokens" if client_options else "max_completion_tokens"
    assert calls == [("offline", {cap_key: 4}), ("offline", {cap_key: 4}),
                     ("fallback", expected)]
    assert (supplied, client_options) == original


def test_reasoning_openai_translates_configured_output_cap():
    service, captured = CompletionService(), []
    service._clients["openai"] = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **request: captured.append(request) or {
            "choices": [{"message": reply()["message"], "finish_reason": "stop"}],
        },
    )))
    service.complete(MESSAGES, config({"provider": "openai", "model": "gpt-5-nano", "retries": 0,
                                       "max_output_tokens": 32}), {})
    assert captured[0]["max_completion_tokens"] == 32
    assert "max_tokens" not in captured[0]


@pytest.mark.parametrize("probability", [False, True])
def test_text_router_skips_small_routes_and_passes_each_output_budget(monkeypatch, probability):
    router, calls = Router(), []
    monkeypatch.setattr(router_module.random, "choices", lambda *args, **kwargs: [0])
    monkeypatch.setattr(router_module.time, "sleep", lambda _: pytest.fail("Oversize inputs must not retry"))
    monkeypatch.setattr(router, "_execute_single_provider", lambda **kwargs:
                        calls.append(deepcopy(kwargs)) or "Done")
    supplied = config(route("small", retries=5, max_input_tokens=1, max_output_tokens=4),
                      route("large", max_input_tokens=1000, max_output_tokens=70), probability=probability)
    original = deepcopy((MESSAGES, supplied))
    result = router.route_chat(messages=MESSAGES, config=supplied)
    assert result.model == "large"
    assert [(call["model"], call["max_output_tokens"], call["messages"]) for call in calls] == [
        ("large", 70, MESSAGES),
    ]
    assert (MESSAGES, supplied) == original


def test_text_router_fallback_uses_unconfigured_output_default_and_full_system_prompt(monkeypatch):
    router, calls = Router(), []

    def execute(**kwargs):
        calls.append(kwargs)
        if kwargs["model"] == "offline":
            raise ChatAPIError("Offline")
        return "Done"

    monkeypatch.setattr(router, "_execute_single_provider", execute)
    supplied = config(route("small", max_input_tokens=100), route("offline", max_output_tokens=4),
                      route("fallback"))
    prompt = "instruction " * 1000
    result = router.route_chat(messages=MESSAGES, system_prompt=prompt, config=supplied)
    assert result.model == "fallback"
    assert [(item["model"], item["max_output_tokens"]) for item in calls] == [("offline", 4), ("fallback", -1)]
    assert all(item["system_prompt"] == prompt and item["messages"] == MESSAGES for item in calls)


def test_model_limits_persist_in_library_downloads_and_independent_session_snapshots():
    supplied = config(route(max_input_tokens=4096, max_output_tokens=512),
                      route("fallback", max_input_tokens=32000, max_output_tokens=2048), probability=True)
    created = client.post("/api/configs", json={"name": "Token budgets", "model_id": "budgets", "config": supplied})
    assert created.status_code == 201, created.text
    record = created.json()
    path = f"/api/configs/{record['id']}"
    expected = normalize_config(supplied)
    assert client.get(path).json()["config"] == expected
    assert client.get(path + "/download").json() == expected
    session = client.post("/api/sessions", json={"config_id": record["id"]})
    assert session.status_code == 201, session.text
    session_path = f"/api/sessions/{session.json()['session_id']}"
    assert client.get(session_path).json()["config"] == expected
    edited = deepcopy(expected)
    edited["sequences"][0]["choices"][0].update(max_input_tokens=8192, max_output_tokens=None)
    patched = client.patch(session_path, json={"config": edited})
    assert patched.status_code == 200, patched.text
    assert client.get(session_path).json()["config"] == normalize_config(edited)
    assert client.get(path).json()["config"] == expected
    updated = client.patch(path, json={"config": edited})
    assert updated.status_code == 200, updated.text
    assert client.get(path + "/download").json() == normalize_config(edited)


def test_invalid_token_budget_api_updates_preserve_saved_configuration():
    supplied = config(route(max_input_tokens=4096, max_output_tokens=512))
    session = client.post("/api/sessions", json={"config": supplied})
    assert session.status_code == 201, session.text
    session_path = f"/api/sessions/{session.json()['session_id']}"
    library = client.post("/api/configs", json={"name": "Budgets", "model_id": "budgets", "config": supplied})
    assert library.status_code == 201, library.text
    library_path = f"/api/configs/{library.json()['id']}"
    invalid = config(route(max_input_tokens=True, max_output_tokens=0))
    assert client.post("/api/config/validate", json=invalid).status_code == 422
    for path in (session_path, library_path):
        assert client.patch(path, json={"config": invalid}).status_code == 422
        assert client.get(path).json()["config"] == normalize_config(supplied)


@pytest.mark.parametrize("database_free", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_oversized_compatibility_request_returns_client_context_error(monkeypatch, database_free, stream):
    monkeypatch.setattr(compatibility.settings, "completion_no_database_access", database_free)
    created = client.post("/api/configs", json={
        "name": "Small context", "model_id": "small-context", "config": config(route(max_input_tokens=1)),
    })
    assert created.status_code == 201, created.text
    monkeypatch.setattr(compatibility.completion_service, "_execute",
                        lambda *args: pytest.fail("Provider must not receive oversized input"))
    result = client.post("/v1/chat/completions", json={
        "model": "small-context", "messages": MESSAGES, "stream": stream,
    })
    assert result.status_code == 400, result.text
    assert result.json()["error"]["code"] == "context_length_exceeded"
    assert result.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.parametrize("endpoint,prompt_field", [("/api/chat", "message"), ("/api/agent/run", "prompt")])
def test_oversized_session_and_agent_requests_return_client_error(monkeypatch, endpoint, prompt_field):
    supplied = {**config(route(max_input_tokens=1)), "past_memory": False}
    created = client.post("/api/sessions", json={"config": supplied, "user_session": True})
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]
    monkeypatch.setattr(main.router, "_execute_single_provider",
                        lambda **kwargs: pytest.fail("Provider must not receive oversized input"))
    result = client.post(endpoint, json={"session_id": session_id, prompt_field: "Hello"})
    assert result.status_code == 400, result.text
    assert "exceeding the input limit" in result.json()["detail"]
    assert client.get(f"/api/sessions/{session_id}").json()["messages"] == []
