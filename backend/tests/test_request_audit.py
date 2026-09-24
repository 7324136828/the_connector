"""Requests and routing remain private diagnostics rather than user memory."""

import asyncio
from contextlib import closing
import io
import json
import sqlite3
import zipfile

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.api import compatibility
from backend.app.audit_store import AuditStore
from backend.app.response_logging import ResponseLoggingMiddleware, ResponseLogWriter


def audit_records(path=None):
    with closing(sqlite3.connect(path or main.internal_audit_store.db_path)) as connection:
        return [json.loads(row[0]) for row in connection.execute("SELECT record_json FROM internal_api_audit ORDER BY rowid")]


def mock_config():
    return {"sequences": [{"provider": "mock", "model": "mock-assistant", "retries": 0}]}


def test_actual_fallback_and_upstream_model_are_private_and_persisted(monkeypatch):
    def execute(route, messages, options):
        if route["model"] == "first":
            raise RuntimeError("Private provider error details")
        return {"message": {"role": "assistant", "content": "Success"}, "finish_reason": "stop", "model": "upstream-version-3"}

    monkeypatch.setattr(compatibility.completion_service, "_execute", execute)
    with TestClient(main.app) as client:
        library = client.post("/api/configs", json={
            "name": "Fallback", "model_id": "my-config",
            "config": {"sequences": [
                {"provider": "mock", "model": "first", "retries": 1},
                {"provider": "mock", "model": "backup", "retries": 0},
            ]},
        }).json()
        for stream in (False, True):
            payload = {"model": "my-config", "messages": [{"role": "user", "content": "Trace this request"}], "stream": stream}
            response = client.post("/api/chat/completions", json=payload)
            assert response.status_code == 200
            record = audit_records()[-1]
            assert record["request"]["body"] == payload
            assert record["routing"]["requested_model"] == "my-config"
            assert record["routing"]["configuration_id"] == library["id"]
            assert record["routing"]["performed"] is True
            assert record["routing"]["selected"] == {
                "provider": "mock", "configured_model": "backup", "model": "upstream-version-3", "effort": None,
            }
            attempts = record["routing"]["attempts"]
            assert [attempt["status"] for attempt in attempts] == ["failed", "failed", "success"]
            assert [attempt["attempt"] for attempt in attempts] == [1, 2, 1]
            assert "Private provider error details" not in json.dumps(record)
            if stream:
                assert record["body"] == response.text
                assert "data: [DONE]" in record["body"]
            else:
                assert response.json()["model"] == "my-config"
                assert "routing" not in response.json()


def test_session_chat_routing_is_correlated_with_request():
    with TestClient(main.app) as client:
        session_id = client.post(
            "/api/sessions", json={"config": mock_config(), "user_session": True}
        ).json()["session_id"]
        response = client.post("/api/chat", json={"session_id": session_id, "message": "Hello"})
    assert response.status_code == 200
    record = audit_records()[-1]
    assert record["request"]["body"]["message"] == "Hello"
    assert record["routing"]["session_id"] == session_id
    assert record["routing"]["selected"]["provider"] == response.json()["provider"] == "mock"
    assert record["routing"]["selected"]["model"] == response.json()["model"]


def test_diagnostics_are_not_in_conversation_memory_history_exports_or_api():
    marker = "INTERNAL_AUDIT_ONLY_73d58"
    with TestClient(main.app) as client:
        client.post("/api/config/validate", json={**mock_config(), "system_prompt": marker})
        assert marker in json.dumps(audit_records())
        assert audit_records()[-1]["routing"]["performed"] is False
        assert audit_records()[-1]["routing"]["selected"] is None
        session_id = client.post("/api/sessions", json={"config": mock_config()}).json()["session_id"]
        assert marker not in json.dumps(main.session_manager.get_context_window(session_id))
        for path in ("/api/config-history", "/api/configs", "/api/sessions", f"/api/sessions/{session_id}"):
            assert marker not in client.get(path).text
        exported = client.get(f"/api/sessions/{session_id}/export-zip")
        assert exported.status_code == 200
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            for name in archive.namelist():
                assert marker.encode() not in archive.read(name)
        for path in ("/api/audit", "/api/internal-api-audit", "/api/internal_api_audit"):
            assert client.get(path).status_code == 404
        assert not any("audit" in path for path in client.get("/openapi.json").json()["paths"])


def test_database_and_file_failures_are_independent(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("Simulated write failure")

    with TestClient(main.app) as client:
        with monkeypatch.context() as patch:
            patch.setattr(main.response_log_writer, "write", fail)
            assert client.get("/api/models").status_code == 200
            assert audit_records()[-1]["path"] == "/api/models"
        with monkeypatch.context() as patch:
            patch.setattr(main.internal_audit_store, "write", fail)
            assert client.get("/api/sessions").status_code == 200
            assert (main.response_log_writer.directory / "get_api_sessions.jsonl").is_file()
        with monkeypatch.context() as patch:
            patch.setattr(main.response_log_writer, "enabled", False)
            assert client.get("/api/configs").status_code == 200
            assert audit_records()[-1]["path"] == "/api/configs"


def test_chunked_request_is_forwarded_intact_and_limit_is_only_for_capture(tmp_path):
    body = '{"prompt":"A long Unicode request: 配置"}'.encode()
    chunks = [body[:12], body[12:]]
    pending = [{"type": "http.request", "body": part, "more_body": i == 0} for i, part in enumerate(chunks)]
    forwarded = []

    async def receive():
        return pending.pop(0)

    async def send(message):
        pass

    async def application(scope, receive, send):
        for _ in chunks:
            message = await receive()
            forwarded.append(message["body"])
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    store = AuditStore(tmp_path / "audit.db")
    middleware = ResponseLoggingMiddleware(application, ResponseLogWriter(tmp_path / "logs"), audit_store=store, max_request_body_bytes=10)
    scope = {"type": "http", "method": "POST", "path": "/api/chat", "query_string": b"active_only=true&api_key=secret",
             "headers": [(b"content-type", b"application/json"), (b"transfer-encoding", b"chunked"), (b"authorization", b"Bearer sensitive")]}
    asyncio.run(middleware(scope, receive, send))
    assert forwarded == chunks
    record = audit_records(store.db_path)[0]
    assert record["request"]["body"] == body[:10].decode()
    assert record["request"]["truncated"] is True
    assert record["request"]["body_bytes"] == len(body)
    assert record["request"]["complete"] is True
    assert record["request"]["query"] == [{"name": "active_only", "value": "true"}, {"name": "api_key", "value": "[REDACTED]"}]
    assert "sensitive" not in json.dumps(record)
    assert "secret" not in json.dumps(record)


def test_malformed_json_request_and_error_response_are_both_stored():
    with TestClient(main.app) as client:
        response = client.post("/api/configs", content='{ "broken": ', headers={"content-type": "application/json"})
    assert response.status_code == 422
    record = audit_records()[-1]
    assert record["request"]["body"] == '{ "broken": '
    assert record["body"] == response.json()
    assert record["routing"]["performed"] is False
