"""HTTP response logs preserve JSON, errors, and streaming behavior."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from backend.app.response_logging import (
    LoggedFastAPI,
    ResponseLoggingMiddleware,
    ResponseLogWriter,
    route_log_file,
)


def records(directory: Path, filename: str) -> list[dict]:
    path = directory / filename
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def scope(method="POST", path="/api/chat/completions"):
    return {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": b"", "headers": [],
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
    }


async def receive():
    return {"type": "http.request", "body": b"", "more_body": False}


@pytest.mark.parametrize("method,path,filename", [
    ("GET", "/api/config-history", "get_api_config-history.jsonl"),
    ("GET", "/api/configs", "get_api_configs.jsonl"),
    ("GET", "/api/models", "get_api_models.jsonl"),
    ("GET", "/api/providers/models", "get_api_providers_models.jsonl"),
    ("GET", "/api/sessions", "get_api_sessions.jsonl"),
    ("GET", "/api/sessions/a-session", "get_api_sessions_session_id.jsonl"),
    ("GET", "/api/v1/models", "get_api_v1_models.jsonl"),
    ("GET", "/api/v1/models/custom-route", "get_api_v1_models_model_id.jsonl"),
    ("POST", "/api/api/show", "post_api_api_show.jsonl"),
    ("POST", "/api/chat", "post_api_chat.jsonl"),
    ("POST", "/api/chat/completions", "post_api_chat_completions.jsonl"),
    ("POST", "/api/config-history", "post_api_config-history.jsonl"),
    ("POST", "/api/config/validate", "post_api_config_validate.jsonl"),
    ("POST", "/api/configs", "post_api_configs.jsonl"),
    ("POST", "/api/models/capabilities", "post_api_models_capabilities.jsonl"),
    ("POST", "/api/sessions", "post_api_sessions.jsonl"),
])
def test_requested_routes_have_stable_log_files(method, path, filename):
    assert route_log_file(method, path) == filename
    assert route_log_file(method, path + "/") == filename


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/health"), ("GET", "/"), ("GET", "/favicon.ico"),
    ("GET", "/api/sessions/id/export-zip"), ("GET", "/api/config-history/id"),
    ("GET", "/api/v1/models/name/more"), ("DELETE", "/api/sessions"),
    ("PATCH", "/api/configs"), ("OPTIONS", "/api/models"),
    ("HEAD", "/api/models"), ("POST", "/api/providers/models"),
])
def test_unrequested_methods_and_paths_are_excluded(method, path):
    assert route_log_file(method, path) is None


def test_json_response_and_request_are_logged_without_authentication_details(tmp_path):
    writer = ResponseLogWriter(tmp_path)
    app = LoggedFastAPI(response_log_writer=writer)

    @app.post("/api/configs")
    async def config_response(request: Request):
        await request.json()
        return {"name": "配置", "active": True, "config": {"sequences": []}}

    with TestClient(app) as client:
        response = client.post(
            "/api/configs?token=private-query", json={"ignored": "private-request"},
            headers={"Authorization": "Bearer private-header"},
        )
    assert response.status_code == 200
    [record] = records(tmp_path, "post_api_configs.jsonl")
    assert record["body"] == response.json()
    assert record["body_bytes"] == len(response.content)
    assert record["captured_bytes"] == len(response.content)
    assert record["complete"] is True
    assert record["truncated"] is False
    assert record["status_code"] == 200
    assert record["content_type"].startswith("application/json")
    assert record["method"] == "POST"
    assert record["path"] == "/api/configs"
    assert record["route"] == "/api/configs"
    assert record["duration_ms"] >= 0
    assert str(UUID(record["request_id"])) == record["request_id"]
    assert datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).utcoffset().total_seconds() == 0
    serialized = json.dumps(record)
    assert record["request"]["body"] == {"ignored": "private-request"}
    assert record["request"]["query"] == [{"name": "token", "value": "[REDACTED]"}]
    for private in ["private-query", "private-header"]:
        assert private not in serialized


def test_error_responses_and_unhandled_500_are_captured(tmp_path):
    app = LoggedFastAPI(response_log_writer=ResponseLogWriter(tmp_path))

    class RequiredConfig(BaseModel):
        name: str

    @app.post("/api/configs")
    def create_config(body: RequiredConfig):
        return body

    @app.get("/api/sessions/{session_id}")
    def missing_session(session_id: str):
        raise HTTPException(status_code=404, detail="Session not found")

    @app.get("/api/sessions")
    def unexpected_failure():
        raise RuntimeError("Simulated handler failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        validation = client.post("/api/configs", json={})
        missing = client.get("/api/sessions/missing")
        failure = client.get("/api/sessions")
    assert validation.status_code == 422
    assert missing.status_code == 404
    assert failure.status_code == 500
    for filename, response in [
        ("post_api_configs.jsonl", validation),
        ("get_api_sessions_session_id.jsonl", missing),
    ]:
        [record] = records(tmp_path, filename)
        assert record["status_code"] == response.status_code
        assert record["body"] == response.json()
        assert record["complete"] is True
    assert records(tmp_path, "get_api_sessions_session_id.jsonl")[0]["route"] == "/api/sessions/{session_id}"
    [record] = records(tmp_path, "get_api_sessions.jsonl")
    assert record["status_code"] == 500
    assert record["body"] == failure.text == "Internal Server Error"
    assert record["complete"] is True
    assert record["error_type"] == "RuntimeError"


def test_existing_openai_validation_handler_is_logged_unchanged(tmp_path):
    from backend.app import main

    with TestClient(main.app) as client:
        response = client.post("/api/chat/completions", json={"messages": []})
    assert response.status_code == 400
    [record] = records(main.response_log_writer.directory, "post_api_chat_completions.jsonl")
    assert record["status_code"] == 400
    assert record["body"] == response.json()
    assert record["body"]["error"]["type"] == "invalid_request_error"


def test_stream_chunks_forward_immediately_and_reassemble_split_utf8(tmp_path):
    sent = []
    body = 'data: {"text":"Hello 😀"}\n\ndata: [DONE]\n\n'.encode()
    split = body.index("😀".encode()) + 2
    chunks = [body[:split], body[split:]]

    async def send(message):
        sent.append(message)

    async def streaming_app(http_scope, http_receive, http_send):
        await http_send({"type": "http.response.start", "status": 200,
                         "headers": [(b"content-type", b"text/event-stream; charset=utf-8")]})
        await http_send({"type": "http.response.body", "body": chunks[0], "more_body": True})
        # This assertion detects buffering that a TestClient response would conceal.
        assert sent[-1]["body"] == chunks[0]
        await http_send({"type": "http.response.body", "body": chunks[1], "more_body": False})

    middleware = ResponseLoggingMiddleware(streaming_app, ResponseLogWriter(tmp_path))
    asyncio.run(middleware(scope(), receive, send))
    assert [message["body"] for message in sent if message["type"] == "http.response.body"] == chunks
    [record] = records(tmp_path, "post_api_chat_completions.jsonl")
    assert record["body"] == body.decode()
    assert record["body_bytes"] == record["captured_bytes"] == len(body)
    assert record["complete"] is True
    assert record["truncated"] is False


def test_midstream_error_preserves_partial_response_and_exception(tmp_path):
    sent = []

    async def send(message):
        sent.append(message)

    async def failing_app(http_scope, http_receive, http_send):
        await http_send({"type": "http.response.start", "status": 200,
                         "headers": [(b"content-type", b"text/event-stream")]})
        await http_send({"type": "http.response.body", "body": b"data: partial\n\n", "more_body": True})
        raise RuntimeError("Stream interrupted")

    middleware = ResponseLoggingMiddleware(failing_app, ResponseLogWriter(tmp_path))
    with pytest.raises(RuntimeError, match="Stream interrupted"):
        asyncio.run(middleware(scope(), receive, send))
    assert len(sent) == 2
    [record] = records(tmp_path, "post_api_chat_completions.jsonl")
    assert record["body"] == "data: partial\n\n"
    assert record["status_code"] == 200
    assert record["complete"] is False
    assert record["error_type"] == "RuntimeError"


def test_log_write_failure_does_not_change_response(tmp_path, monkeypatch):
    writer = ResponseLogWriter(tmp_path)

    def fail(filename, record):
        raise OSError("Simulated full disk")

    monkeypatch.setattr(writer, "write", fail)
    app = LoggedFastAPI(response_log_writer=writer)

    @app.get("/api/models")
    def models():
        return {"data": []}

    with TestClient(app) as client:
        response = client.get("/api/models")
    assert response.status_code == 200
    assert response.json() == {"data": []}


def test_optional_capture_limit_does_not_truncate_http_response(tmp_path):
    app = LoggedFastAPI(response_log_writer=ResponseLogWriter(tmp_path), response_log_max_body_bytes=10)

    @app.get("/api/models")
    def models():
        return {"data": ["a" * 100]}

    with TestClient(app) as client:
        response = client.get("/api/models")
    assert response.json() == {"data": ["a" * 100]}
    [record] = records(tmp_path, "get_api_models.jsonl")
    assert record["body_bytes"] == len(response.content)
    assert record["captured_bytes"] == 10
    assert record["body"] == response.content[:10].decode()
    assert record["truncated"] is True
    assert record["complete"] is True


def test_disabled_logging_and_unselected_paths_produce_no_records(tmp_path):
    directory = tmp_path / "disabled"
    app = LoggedFastAPI(response_log_writer=ResponseLogWriter(directory, enabled=False))

    @app.get("/api/models")
    def models():
        return {"data": []}

    with TestClient(app) as client:
        assert client.get("/api/models").status_code == 200
    assert list(directory.glob("*.jsonl")) == []

    enabled = LoggedFastAPI(response_log_writer=ResponseLogWriter(tmp_path / "enabled"))

    @enabled.get("/api/health")
    def health():
        return {"status": "ok"}

    with TestClient(enabled) as client:
        assert client.get("/api/health").status_code == 200
    assert list((tmp_path / "enabled").glob("*.jsonl")) == []


def test_rotation_keeps_latest_complete_json_records(tmp_path):
    writer = ResponseLogWriter(tmp_path, max_bytes=200, backup_count=2)
    filename = "get_api_models.jsonl"
    for index in range(6):
        writer.write(filename, {"index": index, "body": "x" * 130})
    files = list(tmp_path.glob(filename + "*"))
    files = [path for path in files if path.name == filename or path.suffix[1:].isdigit()]
    assert len(files) == 3
    retained = [json.loads(line) for path in files for line in path.read_text(encoding="utf-8").splitlines()]
    assert {record["index"] for record in retained} == {3, 4, 5}
    assert records(tmp_path, filename)[-1]["index"] == 5


def test_concurrent_writers_and_rotation_keep_complete_records(tmp_path):
    writers = [ResponseLogWriter(tmp_path, max_bytes=400, backup_count=40) for _ in range(2)]
    filename = "get_api_models.jsonl"

    def write(index):
        writers[index % 2].write(filename, {"index": index, "body": "配置" * 30})

    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(write, range(40)))
    files = list(tmp_path.glob(filename + "*"))
    files = [path for path in files if path.name == filename or path.suffix[1:].isdigit()]
    retained = [json.loads(line) for path in files for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(retained) == 40
    assert {record["index"] for record in retained} == set(range(40))
