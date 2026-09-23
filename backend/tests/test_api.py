"""API regressions for config ownership, routing, effort, and saved memory."""
from copy import deepcopy
import pytest
from fastapi.testclient import TestClient
from backend.app import main
from backend.app.services.router import RouteResult, Router
from backend.app.services.connectors.common import ChatAPIError

client = TestClient(main.app)
MOCK_CONFIG = {"sequences": [{"provider": "mock", "model": "mock-assistant", "retries": 0}]}


def session(config=None, **fields):
    response = client.post("/api/sessions", json={"config": deepcopy(config or MOCK_CONFIG), **fields})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def test_health_and_config_download():
    assert client.get("/api/health").json()["status"] == "ok"
    example = client.get("/api/config/example")
    assert example.status_code == 200
    assert 'filename="config.json"' in example.headers["content-disposition"]
    assert client.post("/api/config/validate", json=example.json()).status_code == 200
    models = client.get("/api/providers/models").json()
    reasoning = next(m for m in models if m["id"] == "gpt-5-nano")
    assert reasoning["effort_levels"] == ["minimal", "low", "medium", "high"]
    assert next(m for m in models if m["id"] == "gpt-4o-mini")["effort_levels"] == []
    assert client.post("/api/config", json=MOCK_CONFIG).status_code == 404
    aliases = client.post("/api/models/capabilities", json={"provider": "openrouter", "model": "anthropic/claude-opus-4.6"})
    assert aliases.json() == {
        "effort_levels": ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
        "default_effort": None,
    }
    arbitrary = client.post("/api/models/capabilities", json={"provider": "openrouter", "model": "vendor/future-model"})
    assert arbitrary.json()["effort_levels"] == ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
    snapshot = client.post("/api/models/capabilities", json={"provider": "openai", "model": "gpt-5.2-2025-12-11"})
    assert snapshot.json()["effort_levels"] == ["none", "low", "medium", "high", "xhigh"]


@pytest.mark.parametrize("payload", [{}, {"provider": "mock"}, {"config": None}, {"config": MOCK_CONFIG, "model": "override"}])
def test_config_is_required_and_only_settings_source(payload):
    for endpoint in ("/api/sessions", "/api/new"):
        assert client.post(endpoint, json=payload).status_code == 422


@pytest.mark.parametrize("config", [
    {}, {"sequences": []}, {"sequences": [None]},
    {"sequences": [{"provider": "unknown", "model": "x"}]},
    {"sequences": [{"provider": "mock", "model": ""}]},
    {"sequences": [{"provider": "mock", "model": "x", "retries": -1}]},
    {"sequences": [{"provider": "mock", "model": "x", "effort": "high"}]},
    {"sequences": [{"provider": "openai", "model": "gpt-5-nano", "effort": "easy"}]},
    {"sequences": [{"type": "probability", "choices": []}]},
    {"sequences": [{"type": "probability", "choices": [{"provider": "mock", "model": "x", "probability": 0}]}]},
    {**MOCK_CONFIG, "context_window": 0}, {**MOCK_CONFIG, "past_memory": "false"},
    {**MOCK_CONFIG, "memory_window": -1}, {**MOCK_CONFIG, "memory_scope": "all"},
    {**MOCK_CONFIG, "effort": "high"},
])
def test_invalid_config_rejected_before_session_creation(config):
    assert client.post("/api/sessions", json={"config": config}).status_code == 422
    assert client.post("/api/config/validate", json=config).status_code == 422
    assert client.get("/api/sessions").json() == []


def test_config_round_trip_and_session_isolation():
    config = {"sequences": [{"provider": "openai", "model": "gpt-5-nano", "effort": "high"}],
              "system_prompt": "Be precise.", "context_window": 5}
    first, second = session(config), session()
    detail = client.get(f"/api/sessions/{first}").json()
    assert detail["config"]["sequences"][0]["effort"] == "high"
    assert detail["system_prompt"] == "Be precise."
    edited = {**detail["config"], "past_memory": False}
    patched = client.patch(f"/api/sessions/{first}", json={"config": edited})
    assert patched.status_code == 200
    assert patched.json()["session"]["past_memory"] is False
    assert client.get(f"/api/sessions/{second}").json()["session"]["past_memory"] is True
    assert client.get(f"/api/sessions/{first}").json()["config"] == edited


def test_openrouter_effort_is_optional_for_every_model():
    omitted = client.post("/api/config/validate", json={
        "sequences": [{"provider": "openrouter", "model": "vendor/future-model", "effort": None}],
    })
    assert omitted.status_code == 200, omitted.text
    assert "effort" not in omitted.json()["sequences"][0]
    explicit = client.post("/api/config/validate", json={
        "sequences": [{"provider": "openrouter", "model": "vendor/future-model", "effort": "minimal"}],
    })
    assert explicit.status_code == 200, explicit.text
    assert explicit.json()["sequences"][0]["effort"] == "minimal"


def test_chat_and_legacy_alias():
    created = client.post("/api/new", json={"title": "Test", "config": MOCK_CONFIG})
    assert created.status_code == 201
    sid = created.json()["session_id"]
    result = client.post("/api/chat", json={"session_id": sid, "message": "Hello"})
    assert result.status_code == 200
    assert result.json()["provider"] == "mock"
    assert result.json()["tokens"]["total_tokens"] > 0
    assert len(client.get(f"/api/sessions/{sid}").json()["messages"]) == 2
    for override in [{"provider": "openai"}, {"model": "other"}, {"config": MOCK_CONFIG}, {"past_memory": False}]:
        assert client.post("/api/chat", json={"session_id": sid, "message": "test", **override}).status_code == 422


def test_chat_memory_reaches_provider_and_off_survives_reload(monkeypatch):
    calls = []
    def complete(**kwargs):
        calls.append(kwargs)
        return RouteResult("Acknowledged.", "mock", "mock-assistant", {"total_tokens": 1}, 1, {})
    monkeypatch.setattr(main.router, "route_chat", complete)
    old = session()
    assert client.post("/api/chat", json={"session_id": old, "message": "My garden is named Juniper."}).status_code == 200
    client.delete(f"/api/sessions/{old}")
    current = session()
    client.post("/api/chat", json={"session_id": current, "message": "What do you remember?"})
    assert "My garden is named Juniper." in calls[-1]["system_prompt"]
    assert old in calls[-1]["system_prompt"]
    detail = client.get(f"/api/sessions/{current}").json()
    client.patch(f"/api/sessions/{current}", json={"config": {**detail["config"], "past_memory": False}})
    client.post("/api/chat", json={"session_id": current, "message": "And now?"})
    assert "Juniper" not in calls[-1]["system_prompt"]
    assert calls[-1]["messages"] == [{"role": "user", "content": "And now?"}]


def test_failed_chat_does_not_pollute_memory(monkeypatch):
    sid = session()
    def fail(**kwargs):
        raise ChatAPIError("Provider unavailable")
    monkeypatch.setattr(main.router, "route_chat", fail)
    assert client.post("/api/chat", json={"session_id": sid, "message": "retry me"}).status_code == 500
    assert client.get(f"/api/sessions/{sid}").json()["messages"] == []


def test_closed_and_missing_sessions():
    sid = session()
    assert client.delete(f"/api/close?session_id={sid}").status_code == 200
    assert client.post("/api/chat", json={"session_id": sid, "message": "hi"}).status_code == 409
    assert client.patch(f"/api/sessions/{sid}", json={"config": MOCK_CONFIG}).status_code == 409
    assert client.post("/api/chat", json={"session_id": "missing", "message": "hi"}).status_code == 404


@pytest.mark.parametrize("endpoint,prompt", [("/api/chat", {"message": "test"}), ("/api/agent/run", {"prompt": "test"})])
def test_close_during_inference_has_no_partial_exchange(monkeypatch, endpoint, prompt):
    sid = session()
    def complete(**kwargs):
        main.session_manager.close_session(sid)
        return RouteResult('{"final_answer":"test"}', "mock", "mock-assistant", {"total_tokens": 1}, 1, {})
    monkeypatch.setattr(main.router, "route_chat", complete)
    result = client.post(endpoint, json={"session_id": sid, **prompt})
    assert result.status_code == 409
    assert client.get(f"/api/sessions/{sid}").json()["messages"] == []


def test_agent_uses_session_config_and_memory(monkeypatch):
    calls = []
    def complete(**kwargs):
        calls.append(kwargs)
        return RouteResult('{"final_answer":"Juniper"}', "mock", "actual-model", {"total_tokens": 2}, 1, {})
    monkeypatch.setattr(main.router, "route_chat", complete)
    sid = session()
    main.session_manager.add_message(sid, "user", "Remember Juniper")
    result = client.post("/api/agent/run", json={"session_id": sid, "prompt": "Recall it"})
    assert result.status_code == 200, result.text
    assert result.json()["model"] == "actual-model"
    assert calls[-1]["config"]["sequences"][0]["provider"] == "mock"
    assert any("Juniper" in m["content"] for m in calls[-1]["messages"])
    assert client.post("/api/agent/run", json={"prompt": "test", "provider": "mock"}).status_code == 422
    assert client.post("/api/agent/step", json={"tool": "calculator", "arguments": {"expression": "25 * 4 + 15"}}).json()["result"] == "115"


def test_router_preserves_efforts_on_retries_probability_and_fallback(monkeypatch):
    router = Router()
    calls = []
    def complete(**kwargs):
        calls.append(kwargs)
        if kwargs["model"] == "gpt-5-nano":
            raise ChatAPIError("Unavailable")
        return "OK"
    monkeypatch.setattr(router, "_execute_single_provider", complete)
    monkeypatch.setattr("time.sleep", lambda _: None)
    config = {"sequences": [
        {"type": "probability", "choices": [{"provider": "openai", "model": "gpt-5-nano", "effort": "high", "retries": 1}]},
        {"provider": "openai", "model": "gpt-5-mini", "effort": "medium", "retries": 0},
    ]}
    result = router.route_chat(messages=[{"role": "user", "content": "test"}], config=config)
    assert [c["effort"] for c in calls] == ["high", "high", "medium"]
    assert result.attempt_info["effort"] == "medium"
    with pytest.raises(ChatAPIError, match="All configured routes failed"):
        router.route_chat(messages=[{"role": "user", "content": "test"}], config={"sequences": config["sequences"][:1]})
    with pytest.raises(ChatAPIError, match="config.json"):
        router.route_chat(messages=[{"role": "user", "content": "test"}])
