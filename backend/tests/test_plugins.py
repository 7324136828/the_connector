"""Plugin installation and actual supervised-process integration."""
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import stat
import sys
import zipfile

from fastapi.testclient import TestClient
import pytest

from backend.app import main
from backend.app.services.plugin_manager import PluginManager, PluginError, validate_zip
from plugin.count_message.build_plugin import build

ROOT = Path(__file__).resolve().parents[2]
client = TestClient(main.app)
CONFIG = {"sequences": [{"provider": "mock", "model": "mock-assistant", "retries": 0}]}


@pytest.fixture
def payload(tmp_path):
    target = tmp_path / "plugin.zip"
    build(target)
    return target.read_bytes()


def mutate_archive(payload, *, extra=None, change_manifest=None):
    result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as original, zipfile.ZipFile(result, "w") as archive:
        for item in original.infolist():
            content = original.read(item)
            if item.filename == "plugin.json" and change_manifest:
                manifest = json.loads(content)
                change_manifest(manifest)
                content = json.dumps(manifest).encode()
            archive.writestr(item, content)
        if extra:
            archive.writestr(*extra)
    data = result.getvalue()
    if extra and isinstance(extra[0], str) and "\\" in extra[0]:
        # ZipInfo normalizes separators on Windows; construct the malicious
        # raw ZIP filename to exercise the reader rather than the writer.
        data = data.replace(extra[0].replace("\\", "/").encode(), extra[0].encode())
    return data


def fast_runtime(monkeypatch):
    # Integration runs the actual backend, avoiding network/setup in the test.
    monkeypatch.setattr(main.plugin_manager, "_prepare_runtime", lambda folder: sys.executable)


def install(payload):
    response = client.post("/api/plugins/install", content=payload, headers={"Content-Type": "application/zip"})
    assert response.status_code == 201, response.text
    return response.json()


def test_install_disabled_duplicate_and_uninstall(payload):
    assert client.get("/api/plugins").json() == []
    record = install(payload)
    assert record["status"] == "disabled"
    assert not record["enabled"]
    assert client.post("/api/plugins/install", content=payload).status_code == 409
    assert client.get("/api/plugins/count_message/frame").status_code == 409
    assert client.delete("/api/plugins/count_message").status_code == 200
    assert client.get("/api/plugins").json() == []
    assert not (main.plugin_manager.directory / "count_message").exists()


@pytest.mark.parametrize("path", ["../escape.py", "/absolute.py", "C:/escape.py", "ui/../../../escape.py", "ui\\escape.js", "ui/NUL.js", ".venv/ready"])
def test_reject_unsafe_archives(payload, path):
    response = client.post("/api/plugins/install", content=mutate_archive(payload, extra=(path, b"danger")))
    assert response.status_code == 400
    assert client.get("/api/plugins").json() == []


def test_reject_symlinks_invalid_language_and_missing_layout(payload):
    entry = zipfile.ZipInfo("ui/link.js")
    entry.create_system = 3
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    for invalid in [b"not a zip", mutate_archive(payload, extra=(entry, b"../../private")),
                    mutate_archive(payload, change_manifest=lambda m: m["backend"].update(language="java"))]:
        assert client.post("/api/plugins/install", content=invalid).status_code == 400
    assert client.post("/api/plugins/install", content=b"x" * (10 * 1024 * 1024 + 1)).status_code == 413


def test_enable_proxy_agent_ui_disable_and_persistence(payload, monkeypatch, isolate_api_database):
    manager = isolate_api_database
    session = client.post("/api/sessions", json={"config": CONFIG, "user_session": True}).json()["session_id"]
    manager.add_message(session, "user", "hello")
    manager.add_message(session, "assistant", "hi")
    install(payload)
    fast_runtime(monkeypatch)
    enabled = client.post("/api/plugins/count_message/enable")
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["status"] == "running"
    process = main.plugin_manager._running["count_message"]["process"]
    assert client.post("/api/plugins/count_message/enable").status_code == 200
    assert main.plugin_manager._running["count_message"]["process"] is process
    proxied = client.get("/api/plugins/count_message/proxy/api/count_message", params={"session_id": session})
    assert proxied.status_code == 200, proxied.text
    assert proxied.json()["total_messages"] == 2
    assert proxied.headers["access-control-allow-origin"] == "*"
    tool = next(tool for tool in client.get("/api/agent/tools").json() if tool["name"] == "count_message")
    assert "session_id" in tool["parameters"]["properties"]
    invoked = client.post("/api/agent/step", json={"tool": "count_message", "arguments": {"session_id": session}}).json()
    assert invoked["success"]
    assert invoked["result"]["total_messages"] == 2
    frame = client.get("/api/plugins/count_message/frame", params={"session_id": session})
    assert frame.status_code == 200
    assert '"sessionId": "' + session in frame.text
    asset = client.get("/api/plugins/count_message/assets/ui/src/index.js", headers={"Origin": "null"})
    assert asset.status_code == 200
    assert asset.headers["access-control-allow-origin"] == "null"
    preflight = client.options("/api/plugins/count_message/proxy/api/count_message", headers={
        "Origin": "null", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "Content-Type",
    })
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "null"
    opaque_post = client.post("/api/plugins/count_message/proxy/api/count_message", json={}, headers={"Origin": "null"})
    assert opaque_post.status_code == 200
    assert opaque_post.headers["access-control-allow-origin"] == "null"
    assert client.get("/api/plugins/count_message/assets/backend/count_message/server.py").status_code == 404
    assert client.get("/api/plugins/count_message/proxy/undeclared").status_code == 404
    assert client.get("/api/plugins/count_message/proxy/api/count_message?session_id=missing").status_code == 404
    main.plugin_manager.shutdown()
    assert process.poll() is not None
    # Registry intent survives shutdown and a new manager restores the tool.
    restarted = PluginManager(manager.db_path, main.plugin_manager.directory, lambda: main.agent_service)
    monkeypatch.setattr(restarted, "_prepare_runtime", lambda folder: sys.executable)
    try:
        restarted.restore()
        assert restarted.get("count_message")["status"] == "running"
        assert restarted.invoke("count_message", {})["total_messages"] == 2
        restarted.disable("count_message")
        assert not restarted.get("count_message")["enabled"]
        assert not any(tool.name == "count_message" for tool in main.agent_service.list_tools())
    finally:
        restarted.shutdown()


def test_failure_keeps_connector_usable_and_retry_works(payload, monkeypatch):
    install(payload)
    def fail(folder):
        raise PluginError("Could not create environment")
    monkeypatch.setattr(main.plugin_manager, "_prepare_runtime", fail)
    assert client.post("/api/plugins/count_message/enable").status_code == 400
    record = client.get("/api/plugins").json()[0]
    assert record["status"] == "error"
    assert record["error"] == "Could not create environment"
    assert client.get("/api/sessions").status_code == 200
    fast_runtime(monkeypatch)
    assert client.post("/api/plugins/count_message/enable").json()["status"] == "running"
    assert client.post("/api/plugins/count_message/disable").json()["status"] == "disabled"


def test_tool_name_collision_preserves_existing_tool(payload, monkeypatch):
    install(payload)
    main.agent_service.register_tool("count_message", "Existing tool", {"type": "object"}, handler=lambda args: "existing")
    fast_runtime(monkeypatch)
    assert client.post("/api/plugins/count_message/enable").status_code == 409
    main.plugin_manager.disable("count_message")
    assert main.agent_service.execute_tool("count_message", {}).result == "existing"


def test_managed_environment_runs_count_message_without_pip(payload):
    install(payload)
    enabled = client.post("/api/plugins/count_message/enable")
    assert enabled.status_code == 200, enabled.text
    folder = main.plugin_manager.directory / "count_message"
    assert (folder / ".venv/ready").exists()
    assert client.get("/api/plugins/count_message/proxy/api/count_message").json()["total_messages"] == 0


def test_opaque_frame_cors_is_scoped_to_plugin_resources():
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from backend.app.api.plugins import PluginFrameCORSMiddleware
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5130"], allow_methods=["POST"])
    app.add_middleware(PluginFrameCORSMiddleware)
    headers = {"Origin": "null", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "Content-Type"}
    with TestClient(app) as isolated:
        assert isolated.options("/api/plugins/count_message/proxy/api/count_message", headers=headers).status_code == 200
        assert isolated.options("/api/chat", headers=headers).status_code == 400
        assert isolated.options("/api/plugins/install", headers=headers).status_code == 400
