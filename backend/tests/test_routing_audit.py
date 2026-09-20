"""Private routing metadata follows actual execution without leaking payloads."""

import asyncio
from copy import deepcopy
import importlib
import json
import threading

import anyio
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.app import audit_context as audit
from backend.app.api import compatibility
from backend.app.services.completion_service import CompletionService, UnsupportedCompletionOption
from backend.app.services.connectors import ChatAPIError
from backend.app.services.router import Router


router_module = importlib.import_module("backend.app.services.router")
completion_module = importlib.import_module("backend.app.services.completion_service")
MESSAGES = [{"role": "user", "content": "Private user prompt"}]


@pytest.fixture
def context():
    token = audit.start_audit_context()
    try:
        yield audit.get_audit_context()
    finally:
        audit.finish_audit_context(token)


def route(model="mock-assistant", provider="mock", **extra):
    return {"provider": provider, "model": model, "retries": 0, **extra}


def response(model=None):
    return {"message": {"role": "assistant", "content": "Private assistant response"},
            "finish_reason": "stop", "model": model}


def test_context_lifecycle_metadata_filter_and_nested_restore():
    assert audit.get_audit_context() is None
    audit.set_audit_context(requested_model="ignored")
    audit.record_route_attempt("mock", "ignored", None, "success")
    token = audit.start_audit_context()
    original = audit.get_audit_context()
    try:
        assert original == {"performed": False, "requested_model": None, "configuration_id": None,
                            "session_id": None, "selected": None, "attempts": []}
        audit.set_audit_context(requested_model="alias", configuration_id="configuration-id",
                               session_id="session-id", prompt="must not be retained")
        nested = audit.start_audit_context()
        try:
            audit.set_audit_context(requested_model="nested")
            assert audit.get_audit_context() is not original
        finally:
            audit.finish_audit_context(nested)
        assert audit.get_audit_context() is original
        assert original["requested_model"] == "alias"
        assert original["configuration_id"] == "configuration-id"
        assert original["session_id"] == "session-id"
        assert "prompt" not in original
    finally:
        audit.finish_audit_context(token)
    assert audit.get_audit_context() is None


def test_text_router_logs_retry_fallback_and_actual_success(context, monkeypatch):
    router = Router()
    calls = []

    def execute(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "first":
            raise TimeoutError("Secret provider error details")
        return "Private assistant response"

    monkeypatch.setattr(router, "_execute_single_provider", execute)
    monkeypatch.setattr(router_module.time, "sleep", lambda _: None)
    result = router.route_chat(MESSAGES, config={"sequences": [route("first", retries=1), route("second")]})
    assert calls == ["first", "first", "second"]
    assert result.provider == "mock" and result.model == "second"
    assert context["performed"] is True
    assert context["selected"] == {"provider": "mock", "model": "second", "configured_model": "second", "effort": None}
    assert [(entry["model"], entry["attempt"], entry["status"]) for entry in context["attempts"]] == [
        ("first", 1, "failed"), ("first", 2, "failed"), ("second", 1, "success"),
    ]
    assert context["attempts"][0]["error_type"] == "TimeoutError"
    assert all(entry["elapsed_ms"] >= 0 for entry in context["attempts"])
    serialized = json.dumps(context)
    for secret in ["Private user prompt", "Private assistant response", "Secret provider error details"]:
        assert secret not in serialized


def test_text_router_probability_logs_only_attempted_choice(context, monkeypatch):
    router = Router()
    monkeypatch.setattr(router_module.random, "choices", lambda *args, **kwargs: [1])
    monkeypatch.setattr(router, "_execute_single_provider", lambda **kwargs: "Success")
    result = router.route_chat(MESSAGES, config={"sequences": [{"type": "probability", "choices": [
        {**route("not-picked"), "probability": 1}, {**route("picked"), "probability": 1},
    ]}]})
    assert result.model == "picked"
    assert len(context["attempts"]) == 1
    assert context["attempts"][0]["model"] == "picked"
    assert context["attempts"][0]["routing_type"] == "probability"


def test_exhausted_text_routes_have_no_successful_selection(context, monkeypatch):
    router = Router()

    def fail(**kwargs):
        raise RuntimeError("Private failure")

    monkeypatch.setattr(router, "_execute_single_provider", fail)
    with pytest.raises(ChatAPIError):
        router.route_chat(MESSAGES, config={"sequences": [route("first"), route("second")]})
    assert context["performed"] is True
    assert context["selected"] is None
    assert [entry["status"] for entry in context["attempts"]] == ["failed", "failed"]


def test_structured_routes_log_unsupported_retries_and_upstream_model(context, monkeypatch):
    service = CompletionService()
    seen = []

    def execute(selected, messages, options):
        seen.append(selected["model"])
        if selected["model"] == "unsupported":
            raise UnsupportedCompletionOption("Unsupported private details")
        if selected["model"] == "retry":
            raise ConnectionError("Private connection details")
        return response("gpt-5-nano-upstream-version")

    monkeypatch.setattr(service, "_execute", execute)
    monkeypatch.setattr(completion_module.time, "sleep", lambda _: None)
    result = service.complete(MESSAGES, {"sequences": [
        route("unsupported", retries=2), route("retry", retries=1),
        route("gpt-5-nano", provider="openai", effort="low"),
    ]}, {})
    assert seen == ["unsupported", "retry", "retry", "gpt-5-nano"]
    assert result["model"] == "gpt-5-nano-upstream-version"
    assert context["selected"] == {
        "provider": "openai", "model": "gpt-5-nano-upstream-version",
        "configured_model": "gpt-5-nano", "effort": "low",
    }
    assert [entry["status"] for entry in context["attempts"]] == ["unsupported", "failed", "failed", "success"]
    assert context["attempts"][0]["error_type"] == "UnsupportedCompletionOption"
    assert context["attempts"][-1]["returned_model"] == "gpt-5-nano-upstream-version"
    assert "Private" not in json.dumps(context)


def test_structured_invalid_response_is_a_failed_attempt_before_fallback(context, monkeypatch):
    service = CompletionService()
    monkeypatch.setattr(service, "_execute", lambda selected, *_: (
        {"message": {"role": "assistant", "content": None}} if selected["model"] == "empty" else response()
    ))
    result = service.complete(MESSAGES, {"sequences": [route("empty"), route("working")]}, {})
    assert result["model"] == "working"
    assert context["attempts"][0]["status"] == "failed"
    assert context["attempts"][0]["error_type"] == "ChatAPIError"
    assert context["selected"]["model"] == "working"
    assert "returned_model" not in context["attempts"][1]


def test_unsupported_only_routes_have_no_selected_model(context, monkeypatch):
    service = CompletionService()

    def unsupported(*args):
        raise UnsupportedCompletionOption("Cannot execute")

    monkeypatch.setattr(service, "_execute", unsupported)
    with pytest.raises(UnsupportedCompletionOption):
        service.complete(MESSAGES, {"sequences": [route()]}, {})
    assert context["selected"] is None
    assert context["attempts"][0]["status"] == "unsupported"


def test_invalid_configuration_or_request_does_not_claim_inference(context):
    with pytest.raises(ChatAPIError):
        Router().route_chat(MESSAGES, config={})
    with pytest.raises(UnsupportedCompletionOption):
        CompletionService().complete(MESSAGES, {"sequences": [route()]}, {"unknown": True})
    assert context["performed"] is False
    assert context["selected"] is None
    assert context["attempts"] == []


def test_compatibility_records_requested_alias_and_configuration(context):
    record = compatibility.configuration_manager.create_config(
        "Alias", "my-alias", {"sequences": [route()]},
    )
    result = compatibility.chat_completions(compatibility.CompletionRequest(model="my-alias", messages=MESSAGES))
    assert result["model"] == "my-alias"
    assert "selected" not in result and "attempts" not in result
    assert context["requested_model"] == "my-alias"
    assert context["configuration_id"] == record["id"]
    assert context["selected"]["model"] == "mock-assistant"


def test_missing_alias_and_discovery_do_not_claim_inference(context):
    assert compatibility.list_models() == {"object": "list", "data": []}
    assert context["performed"] is False
    with pytest.raises(compatibility.CompatibilityError):
        compatibility.chat_completions(compatibility.CompletionRequest(model="missing", messages=MESSAGES))
    assert context["requested_model"] == "missing"
    assert context["configuration_id"] is None
    assert context["performed"] is False


def test_fastapi_sync_worker_mutates_same_request_context():
    captured = []
    middleware_threads = []
    endpoint_threads = []

    class CaptureAudit:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.app(scope, receive, send)
            middleware_threads.append(threading.get_ident())
            token = audit.start_audit_context()
            try:
                await self.app(scope, receive, send)
            finally:
                captured.append(deepcopy(audit.get_audit_context()))
                audit.finish_audit_context(token)

    app = FastAPI()
    app.add_middleware(CaptureAudit)

    @app.get("/worker")
    def worker():
        endpoint_threads.append(threading.get_ident())
        audit.set_audit_context(session_id="session-from-worker")
        return CompletionService().complete(MESSAGES, {"sequences": [route()]}, {})

    with TestClient(app) as client:
        assert client.get("/worker").status_code == 200
    assert middleware_threads[0] != endpoint_threads[0]
    assert captured[0]["session_id"] == "session-from-worker"
    assert captured[0]["selected"]["model"] == "mock-assistant"
    assert audit.get_audit_context() is None


def test_concurrent_threaded_routes_have_isolated_contexts():
    barrier = threading.Barrier(2)

    def worker(model):
        barrier.wait(timeout=5)
        CompletionService().complete(MESSAGES, {"sequences": [route(model)]}, {})

    async def task(model):
        token = audit.start_audit_context()
        try:
            audit.set_audit_context(requested_model=model)
            await anyio.to_thread.run_sync(worker, model)
            return deepcopy(audit.get_audit_context())
        finally:
            audit.finish_audit_context(token)

    async def run():
        return await asyncio.gather(task("one"), task("two"))

    first, second = asyncio.run(run())
    assert first["requested_model"] == first["selected"]["model"] == "one"
    assert second["requested_model"] == second["selected"]["model"] == "two"
    assert len(first["attempts"]) == len(second["attempts"]) == 1
    assert audit.get_audit_context() is None


def test_metadata_and_attempt_list_are_bounded_without_losing_selection(context, monkeypatch):
    monkeypatch.setattr(audit, "MAX_AUDIT_ATTEMPTS", 2)
    audit.set_audit_context(requested_model="x" * 10000)
    for index in range(3):
        audit.record_route_attempt("mock", str(index), None, "success", elapsed_ms=float("nan"))
    assert len(context["requested_model"]) == 1024
    assert len(context["attempts"]) == 2
    assert context["attempts_dropped"] == 1
    assert context["selected"]["model"] == "2"
    assert context["attempts"][0]["elapsed_ms"] == 0
    json.dumps(context, allow_nan=False)
