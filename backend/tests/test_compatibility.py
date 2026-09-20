"""Client-facing discovery, config aliases, SDK parsing, and agent tool loops."""
import json
import sqlite3
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from openai import OpenAI

from backend.app import main
from backend.app.api import compatibility
from backend.app.services.connectors.common import ChatAPIError

client = TestClient(main.app)
MOCK = {"sequences": [{"provider": "mock", "model": "mock-assistant", "retries": 0}]}
TOOLS = [{"type": "function", "function": {"name": "lookup", "description": "Lookup an item",
          "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}]


def register(model_id="research-router", active=True, config=None, **kwargs):
    result = client.post("/api/configs", json={"name": "Research routing", "model_id": model_id,
                         "active": active, "config": config or deepcopy(MOCK), **kwargs})
    assert result.status_code == 201, result.text
    return result.json()


@pytest.mark.parametrize("path", ["/v1/models", "/api/v1/models", "/api/models", "/models"])
def test_discovery_only_lists_active_configurations(path):
    assert client.get(path).json() == {"object": "list", "data": []}
    active = register(context_length=64000)
    hidden = register("inactive", active=False)
    models = client.get(path).json()
    assert models["object"] == "list"
    assert [m["id"] for m in models["data"]] == [active["model_id"]]
    model = models["data"][0]
    assert model["context_length"] == 64000
    assert "config" not in model
    assert client.get(path + "/research-router").json() == model
    assert client.get(path + "/inactive").status_code == 404
    assert client.get(path + "/gpt-4o-mini").json()["error"]["code"] == "model_not_found"
    assert client.patch("/api/configs/" + hidden["id"], json={"active": True}).status_code == 200
    assert len(client.get(path).json()["data"]) == 2
    client.delete("/api/configs/" + active["id"])
    assert client.get(path + "/research-router").status_code == 404


def test_requested_probes_return_useful_results_without_fake_models():
    assert client.get("/").json()["models"] == "/v1/models"
    assert client.get("/api/version").json()["version"]
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200 and "image/" in icon.headers["content-type"] and icon.content
    assert client.get("/api/api/tags").json() == {"models": []}
    assert client.post("/api/api/show", json={"model": "gpt-4o-mini"}).status_code == 404
    register("gpt-4o-mini", context_length=128000,
             config={"sequences": [{"provider": "openai", "model": "gpt-4o-mini", "retries": 0}]})
    for path in ("/api/api/tags", "/api/tags"):
        assert client.get(path).json()["models"][0]["name"] == "gpt-4o-mini"
    for path in ("/api/api/show", "/api/show"):
        shown = client.post(path, json={"name": "gpt-4o-mini"}).json()
        assert shown["model_info"]["connector.context_length"] == 128000
    for path in ("/api/props", "/api/v1/props", "/props", "/v1/props"):
        props = client.get(path).json()
        assert props["default_generation_settings"]["n_ctx"] == 128000
        assert props["models"][0]["id"] == "gpt-4o-mini"


def test_unknown_context_is_not_invented():
    register(config={"sequences": [{"provider": "ollama", "model": "my-custom-build"}]})
    assert "context_length" not in client.get("/v1/models").json()["data"][0]
    assert "n_ctx" not in client.get("/api/props").json()["default_generation_settings"]


def test_library_session_is_snapshot_and_inactive_configs_cannot_create_sessions():
    record = register()
    created = client.post("/api/sessions", json={"config_id": record["id"]})
    assert created.status_code == 201, created.text
    sid = created.json()["session_id"]
    initial = client.get("/api/sessions/" + sid).json()["config"]
    client.patch("/api/configs/" + record["id"], json={"config": {**MOCK, "past_memory": False}, "active": False})
    assert client.get("/api/sessions/" + sid).json()["config"] == initial
    assert client.post("/api/sessions", json={"config_id": record["id"]}).status_code == 409
    assert client.post("/api/sessions", json={"config_id": "missing"}).status_code == 404
    assert client.post("/api/sessions", json={"config_id": record["id"], "config": MOCK}).status_code == 422
    # Deleting the template neither deletes nor changes an existing conversation.
    client.delete("/api/configs/" + record["id"])
    assert client.post("/api/chat", json={"session_id": sid, "message": "hello"}).status_code == 200


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/api/v1/chat/completions", "/api/chat/completions", "/chat/completions"])
def test_completion_aliases_return_text_without_creating_web_sessions(path):
    register()
    result = client.post(path, json={"model": "research-router", "messages": [{"role": "user", "content": "Hello"}]})
    assert result.status_code == 200, result.text
    reply = result.json()
    assert reply["object"] == "chat.completion" and reply["model"] == "research-router"
    assert reply["choices"][0]["message"]["content"]
    assert reply["usage"]["total_tokens"] > 0
    assert client.get("/api/sessions").json() == []


def test_completion_response_is_saved_without_the_request_and_reused_as_memory(monkeypatch):
    register()
    first = client.post("/v1/chat/completions", json={
        "model": "research-router", "messages": [{"role": "user", "content": "Remember this output"}],
    })
    assert first.status_code == 200
    with sqlite3.connect(main.session_manager.db_path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(completions_response)")]
        rows = conn.execute("SELECT id, response FROM completions_response").fetchall()
    assert columns == ["id", "response", "created_at"]
    assert len(rows) == 1
    assert rows[0][0] == first.json()["id"]
    assert json.loads(rows[0][1]) == first.json()

    captured = {}

    def complete(messages, config, options):
        captured["system_prompt"] = config["system_prompt"]
        return {"message": {"role": "assistant", "content": "Second answer"},
                "finish_reason": "stop", "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    monkeypatch.setattr(compatibility.completion_service, "complete", complete)
    second = client.post("/v1/chat/completions", json={
        "model": "research-router", "messages": [{"role": "user", "content": "Use memory"}],
    })
    assert second.status_code == 200
    assert first.json()["choices"][0]["message"]["content"] in captured["system_prompt"]
    with sqlite3.connect(main.session_manager.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM completions_response").fetchone()[0] == 2


def test_tool_call_round_trip_and_stream_wire_format():
    register()
    first = client.post("/api/chat/completions", json={"model": "research-router",
        "messages": [{"role": "user", "content": "Look it up"}], "tools": TOOLS,
        "tool_choice": {"type": "function", "function": {"name": "lookup"}}})
    assert first.status_code == 200, first.text
    choice = first.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "lookup"
    messages = [{"role": "user", "content": "Look it up"}, choice["message"],
                {"role": "tool", "tool_call_id": call["id"], "content": "Juniper"}]
    final = client.post("/v1/chat/completions", json={"model": "research-router", "messages": messages,
                        "tools": TOOLS, "stream": True, "stream_options": {"include_usage": True}})
    assert final.status_code == 200 and final.headers["content-type"].startswith("text/event-stream")
    lines = [line[6:] for line in final.text.splitlines() if line.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    chunks = [json.loads(line) for line in lines[:-1]]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert "Juniper" in "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["choices"] == [] and chunks[-1]["usage"]["total_tokens"] > 0


def test_official_openai_sdk_parses_discovery_text_and_streamed_tools():
    register()
    # Real SDK serialization/parsing against the ASGI app; no outbound network.
    sdk = OpenAI(base_url="http://testserver/api/v1", api_key="local-test", http_client=client, max_retries=0)
    assert sdk.models.list().data[0].id == "research-router"
    assert sdk.models.retrieve("research-router").id == "research-router"
    response = sdk.chat.completions.create(model="research-router", messages=[{"role": "user", "content": "test"}])
    assert response.choices[0].message.content
    chunks = list(sdk.chat.completions.create(model="research-router", messages=[{"role": "user", "content": "lookup"}],
                   tools=TOOLS, tool_choice="required", stream=True))
    tool_chunks = [chunk for chunk in chunks if chunk.choices[0].delta.tool_calls]
    assert tool_chunks[0].choices[0].delta.tool_calls[0].index == 0
    assert tool_chunks[0].choices[0].delta.tool_calls[0].function.name == "lookup"
    assert chunks[-1].choices[0].finish_reason == "tool_calls"


@pytest.mark.parametrize("changes", [{"messages": []}, {"n": 2}, {"logprobs": True},
    {"tools": [{"type": "unsupported"}]}, {"tool_choice": "required"},
    {"messages": [{"role": "tool", "tool_call_id": "unknown", "content": "x"}]},
    {"stream_options": {"include_usage": "yes"}}, {"max_tokens": 0}])
def test_malformed_or_unsupported_requests_are_explicit_openai_errors(changes):
    register()
    payload = {"model": "research-router", "messages": [{"role": "user", "content": "hi"}], **changes}
    response = client.post("/v1/chat/completions", json=payload)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["message"]


def test_provider_failure_does_not_become_empty_successful_stream(monkeypatch):
    register()
    def fail(**kwargs):
        raise ChatAPIError("Upstream unavailable")
    monkeypatch.setattr(compatibility.completion_service, "complete", fail)
    result = client.post("/v1/chat/completions", json={"model": "research-router", "messages": [{"role": "user", "content": "test"}], "stream": True})
    assert result.status_code == 502
    assert result.json()["error"]["code"] == "upstream_error"


@pytest.mark.parametrize("message,finish", [({"role": "assistant", "content": ""}, "stop"),
    ({"role": "assistant", "content": None, "refusal": "Cannot comply"}, "stop"),
    ({"role": "assistant", "content": None}, "content_filter")])
def test_valid_empty_stop_and_refusal_are_not_transport_failures(monkeypatch, message, finish):
    register()
    monkeypatch.setattr(compatibility.completion_service, "complete", lambda **kwargs: {
        "message": message, "finish_reason": finish, "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1}})
    result = client.post("/v1/chat/completions", json={"model": "research-router", "messages": [{"role": "user", "content": "test"}]})
    assert result.status_code == 200
    assert result.json()["choices"][0]["message"] == message
