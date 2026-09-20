"""Exercise response logging on every requested application endpoint."""

import json
import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.response_logging import route_log_file


def test_all_requested_endpoint_responses_are_logged():
    config = {"sequences": [{"provider": "mock", "model": "mock-assistant", "retries": 0}]}
    with TestClient(main.app) as client:
        library = client.post("/api/configs", json={
            "name": "Logging fixture", "model_id": "my-config", "config": config,
        })
        first = client.post("/api/sessions", json={"config": config}).json()["session_id"]
        second = client.post("/api/sessions", json={"config": config}).json()["session_id"]
        requests = [
            ("GET", "/api/config-history", None),
            ("GET", "/api/configs", None),
            ("GET", "/api/models", None),
            ("GET", "/api/providers/models", None),
            ("GET", "/api/sessions", None),
            ("GET", f"/api/sessions/{first}", None),
            ("GET", f"/api/sessions/{second}", None),
            ("GET", "/api/v1/models", None),
            ("GET", "/api/v1/models/my-config", None),
            ("POST", "/api/api/show", {"model": "my-config"}),
            ("POST", "/api/chat", {"session_id": first, "message": "Logging test"}),
            ("POST", "/api/chat/completions", {"model": "my-config", "messages": [{"role": "user", "content": "Hello"}]}),
            ("POST", "/api/config-history", {"config": config}),
            ("POST", "/api/config/validate", config),
            ("POST", "/api/configs", {"name": "Another", "model_id": "another", "config": config}),
            ("POST", "/api/models/capabilities", {"provider": "mock", "model": "mock-assistant"}),
            ("POST", "/api/sessions", {"config": config}),
        ]
        assert library.status_code == 201
        for method, path, payload in requests:
            response = client.request(method, path, **({"json": payload} if payload is not None else {}))
            assert response.status_code in (200, 201), (path, response.text)
            log_path = main.response_log_writer.directory / route_log_file(method, path)
            record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
            assert record["method"] == method
            assert record["path"] == path
            assert record["status_code"] == response.status_code
            assert record["body"] == response.json()
            assert record["body_bytes"] == len(response.content)
            assert record["complete"] is True
            assert record["truncated"] is False
            assert record["request"]["body"] == payload
            assert record["request"]["complete"] is True
            with closing(sqlite3.connect(main.internal_audit_store.db_path)) as connection:
                stored = connection.execute(
                    "SELECT record_json FROM internal_api_audit WHERE request_id = ?", (record["request_id"],),
                ).fetchone()
            assert json.loads(stored[0]) == record


def test_application_validation_and_missing_responses_logged():
    with TestClient(main.app) as client:
        for method, path, expected_status in [
            ("GET", "/api/sessions/does-not-exist", 404),
            ("GET", "/api/v1/models/missing-model", 404),
            ("POST", "/api/chat/completions", 400),
            ("POST", "/api/configs", 422),
        ]:
            response = client.request(method, path, **({"json": {}} if method == "POST" else {}))
            assert response.status_code == expected_status
            log_path = main.response_log_writer.directory / route_log_file(method, path)
            record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
            assert record["body"] == response.json()
            assert record["status_code"] == expected_status


def test_application_logs_generated_500(monkeypatch):
    def unavailable():
        raise RuntimeError("Simulated database failure")

    monkeypatch.setattr(main.session_manager, "list_sessions", unavailable)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        response = client.get("/api/sessions")
    assert response.status_code == 500
    record = json.loads((main.response_log_writer.directory / "get_api_sessions.jsonl").read_text(encoding="utf-8"))
    assert record["status_code"] == 500
    assert record["body"] == response.text == "Internal Server Error"
    assert record["complete"] is True
    assert record["error_type"] == "RuntimeError"
