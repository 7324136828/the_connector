"""Portable runtime validation and Connector-specific streaming proxy behavior."""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import socket
from pathlib import Path
from unittest import mock

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import packaged


@pytest.fixture
def public(tmp_path):
    directory = tmp_path / "public"
    directory.mkdir()
    (directory / "index.html").write_text("<h1>Portable frontend</h1>", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=outside-public", encoding="utf-8")
    return directory


@pytest.fixture
def clean_runtime_environment(monkeypatch, tmp_path):
    for name in ("FRONTEND_PORT", "CONNECTOR_BACKEND_PORT", "BACKEND_PORT", "PORT",
                 "BACKEND_HOST", "HOST", "CONNECTOR_RUNTIME_ROOT", "SECRET",
                 "KOKORO_PORT", "KOKORO_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONNECTOR_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("CONNECTOR_RUNTIME_ROOT", str(tmp_path))
    (tmp_path / "port_mapping.json").write_text(
        '{"react_port": 5130, "connector_port": 8130, "kokoro_port": 8131}', encoding="utf-8",
    )


def install_transport(monkeypatch, transport):
    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["timeout"].read is None
        return original(transport=transport, **kwargs)

    monkeypatch.setattr(packaged.httpx, "AsyncClient", client)


def test_runtime_profile_follows_frozen_executable_after_folder_rename(tmp_path, monkeypatch):
    root = tmp_path / "Debug"
    root.mkdir()
    (root / "runtime.json").write_text('{"host": "127.0.0.1"}', encoding="utf-8")
    root = root.rename(root.with_name("RenamedPortable"))
    monkeypatch.setattr(packaged.sys, "frozen", True, raising=False)
    monkeypatch.setattr(packaged.sys, "executable", str(root / "server.exe"))
    assert packaged.runtime_directory() == root.resolve()
    assert packaged.runtime_host(packaged.runtime_directory()) == "127.0.0.1"


@pytest.mark.parametrize("content", ["{", "[]", "{}", '{"host": "localhost"}', '{"host": null}'])
def test_invalid_runtime_profile_has_actionable_error(tmp_path, content):
    profile = tmp_path / "runtime.json"
    profile.write_text(content, encoding="utf-8")
    with pytest.raises(packaged.RunError, match="runtime profile") as error:
        packaged.runtime_host(tmp_path)
    assert str(profile) in str(error.value)


def test_cli_ports_and_saved_json_precedence(clean_runtime_environment, monkeypatch):
    defaults = packaged.parse_args([], default_host="127.0.0.1")
    assert (defaults.host, defaults.frontend_port, defaults.backend_port, defaults.kokoro_port) == (
        "127.0.0.1", 5130, 8130, 8131,
    )
    saved = {"react_port": 4400, "connector_port": 8800, "kokoro_port": 8801, "custom": "preserved"}
    settings_path = Path(os.environ["CONNECTOR_SETTINGS_PATH"])
    settings_path.write_text(json.dumps(saved), encoding="utf-8")
    monkeypatch.setenv("FRONTEND_PORT", "4000")
    monkeypatch.setenv("BACKEND_PORT", "8000")
    monkeypatch.setenv("CONNECTOR_BACKEND_PORT", "9000")
    monkeypatch.setenv("BACKEND_HOST", "0.0.0.0")
    environment = packaged.parse_args([], default_host="127.0.0.1")
    assert (environment.host, environment.frontend_port, environment.backend_port, environment.kokoro_port) == (
        "0.0.0.0", 4400, 8800, 8801,
    )
    cli = packaged.parse_args(["--host", "127.0.0.1", "--frontend", "4001", "--backend-port", "9001",
                               "--kokoro-port", "9002"])
    assert (cli.host, cli.frontend_port, cli.backend_port, cli.kokoro_port) == ("127.0.0.1", 4001, 9001, 9002)
    assert json.loads(settings_path.read_text(encoding="utf-8")) == saved
    for arguments in (["--frontend", "0"], ["--backend", "65536"], ["--backend", "abc"],
                      ["--frontend", "8800"], ["--kokoro", "65536"]):
        with pytest.raises(SystemExit) as error:
            packaged.parse_args(arguments)
        assert error.value.code == 2


def test_initialize_settings_fills_missing_ports_without_needing_public_files(
    tmp_path, clean_runtime_environment, monkeypatch,
):
    monkeypatch.setattr(packaged, "runtime_directory", lambda: tmp_path)
    bind = mock.Mock()
    monkeypatch.setattr(packaged, "bind_listener", bind)
    assert packaged.main(["--initialize-settings"]) == 0
    path = Path(os.environ["CONNECTOR_SETTINGS_PATH"])
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "react_port": 5130, "connector_port": 8130, "kokoro_port": 8131,
    }
    path.write_text('{"react_port": 4400, "custom": true}', encoding="utf-8")
    assert packaged.main(["--initialize-settings"]) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "react_port": 4400, "connector_port": 8130, "kokoro_port": 8131, "custom": True,
    }
    bind.assert_not_called()


def test_explicit_mapping_supplies_missing_ports_and_preserves_saved_values(
    tmp_path, clean_runtime_environment,
):
    mapping = tmp_path / "custom_mapping.json"
    mapping.write_text('{"react_port": 5300, "connector_port": 8300, "kokoro_port": 8301}', encoding="utf-8")
    settings_path = Path(os.environ["CONNECTOR_SETTINGS_PATH"])
    settings_path.write_text('{"react_port": 4400, "unrelated": true}', encoding="utf-8")
    options = packaged.parse_args([], defaults_path=mapping)
    assert (options.frontend_port, options.backend_port, options.kokoro_port) == (4400, 8300, 8301)
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "react_port": 4400, "connector_port": 8300, "kokoro_port": 8301, "unrelated": True,
    }


def test_missing_mapping_cannot_fall_back_even_with_complete_saved_ports(
    tmp_path, clean_runtime_environment,
):
    settings_path = Path(os.environ["CONNECTOR_SETTINGS_PATH"])
    saved = {"react_port": 4400, "connector_port": 8800, "kokoro_port": 8801}
    settings_path.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(packaged.RunError, match="port.*mapping|port_mapping"):
        packaged.parse_args([], defaults_path=tmp_path / "missing" / "port_mapping.json")
    assert json.loads(settings_path.read_text(encoding="utf-8")) == saved


def test_help_does_not_read_mapping_or_create_user_settings(tmp_path, clean_runtime_environment):
    with pytest.raises(SystemExit) as result:
        packaged.parse_args(["--help"], defaults_path=tmp_path / "missing" / "port_mapping.json")
    assert result.value.code == 0
    assert not Path(os.environ["CONNECTOR_SETTINGS_PATH"]).exists()


@pytest.mark.parametrize("external_url", [None, "https://speech.example:9443"])
def test_packaged_kokoro_uses_saved_port_or_preserves_external_url(
    public, clean_runtime_environment, monkeypatch, external_url,
):
    monkeypatch.setattr(packaged, "runtime_directory", lambda: public.parent)
    monkeypatch.setattr(packaged, "bind_listener", mock.Mock(side_effect=packaged.RunError("binding checked")))
    Path(os.environ["CONNECTOR_SETTINGS_PATH"]).write_text('{"kokoro_port": 8551}', encoding="utf-8")
    if external_url:
        monkeypatch.setenv("KOKORO_BASE_URL", external_url)
    with pytest.raises(packaged.RunError, match="binding checked"):
        packaged.main([])
    assert os.environ["KOKORO_BASE_URL"] == (external_url or "http://127.0.0.1:8551")


def test_environment_loaded_beside_executable_without_overriding_process(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKEND_PORT", "9000")
    (tmp_path / ".env").write_text("BACKEND_PORT=8000\nFRONTEND_PORT=4000\n", encoding="utf-8")
    monkeypatch.delenv("FRONTEND_PORT", raising=False)
    monkeypatch.delenv("CONNECTOR_RUNTIME_ROOT", raising=False)
    packaged.load_environment(tmp_path)
    assert os.environ["CONNECTOR_RUNTIME_ROOT"] == str(tmp_path.resolve())
    assert os.environ["BACKEND_PORT"] == "9000"
    assert os.environ["FRONTEND_PORT"] == "4000"


def test_explicit_packaged_paths_resolve_beside_executable(tmp_path, monkeypatch):
    monkeypatch.delenv("CONNECTOR_RUNTIME_ROOT", raising=False)
    for name in ("DATA_DIR", "DB_PATH", "CONFIG_PATH", "PLUGIN_DIR", "RESPONSE_LOG_DIR",
                 "AGENT_PYTHON_ENV_DIR", "AGENT_PYTHON_ENVS_DIR", "JOBS_DIR",
                 "CONNECTOR_SETTINGS_PATH", "SETTINGS_PATH", "CONNECTOR_PYTHON_EXECUTABLE"):
        monkeypatch.setenv(name, "portable/" + name.lower())
    packaged.load_environment(tmp_path)
    for name in ("DATA_DIR", "DB_PATH", "CONFIG_PATH", "PLUGIN_DIR", "RESPONSE_LOG_DIR",
                 "AGENT_PYTHON_ENV_DIR", "AGENT_PYTHON_ENVS_DIR", "JOBS_DIR",
                 "CONNECTOR_SETTINGS_PATH", "SETTINGS_PATH", "CONNECTOR_PYTHON_EXECUTABLE"):
        assert Path(os.environ[name]) == tmp_path / "portable" / name.lower()


def test_second_listener_failure_closes_first_listener(public, clean_runtime_environment, monkeypatch):
    backend = mock.Mock(spec=socket.socket)
    monkeypatch.setattr(packaged, "runtime_directory", lambda: public.parent)
    monkeypatch.setattr(packaged, "bind_listener", mock.Mock(
        side_effect=[backend, packaged.RunError("Cannot bind frontend")],
    ))
    with pytest.raises(packaged.RunError, match="Cannot bind frontend"):
        packaged.main([])
    backend.close.assert_called_once()


def test_frontend_serves_public_files_and_forwards_escaped_api_requests(public, monkeypatch):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(201, stream=UpstreamChunks([b'{"saved": true}']), headers=[
            ("content-type", "application/json"),
            ("set-cookie", "first=1"), ("set-cookie", "second=2"),
            ("connection", "x-hop"), ("x-hop", "remove"), ("x-result", "retained"),
        ])

    install_transport(monkeypatch, httpx.MockTransport(upstream))
    application = packaged.create_frontend_app(public, "http://127.0.0.1:8301")
    with TestClient(application) as client:
        assert "Portable frontend" in client.get("/").text
        assert client.get("/%2e%2e/.env").status_code == 404
        response = client.post("/api/plugins/a%20b/assets/x%2Fy?value=a%2Fb&value=two", content=b"payload",
                               headers={"authorization": "Bearer test", "connection": "x-hop",
                                        "x-hop": "remove", "content-type": "application/octet-stream"})
    assert response.status_code == 201
    assert response.json() == {"saved": True}
    assert response.headers.get_list("set-cookie") == ["first=1", "second=2"]
    assert response.headers["x-result"] == "retained"
    assert "x-hop" not in response.headers
    assert seen[0].url.raw_path == b"/api/plugins/a%20b/assets/x%2Fy?value=a%2Fb&value=two"
    assert seen[0].content == b"payload"
    assert seen[0].headers["authorization"] == "Bearer test"
    assert seen[0].headers["host"] == "127.0.0.1:8301"
    assert "x-hop" not in seen[0].headers


def test_unavailable_backend_returns_json_502(public, monkeypatch):
    def unavailable(request):
        raise httpx.ConnectError("connection refused", request=request)

    install_transport(monkeypatch, httpx.MockTransport(unavailable))
    with TestClient(packaged.create_frontend_app(public, "http://127.0.0.1:8301")) as client:
        response = client.get("/api/settings")
    assert response.status_code == 502
    assert response.json() == {"detail": "Backend is unavailable"}


class UpstreamChunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_completion_chunks_are_forwarded_before_upstream_finishes(public, monkeypatch):
    stream = UpstreamChunks([b"data: first\n\n", b"data: [DONE]\n\n"])
    install_transport(monkeypatch, httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"}),
    ))
    application = packaged.create_frontend_app(public, "http://127.0.0.1:8301")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({"type": "http", "method": "GET", "path": "/v1/chat/completions",
                       "raw_path": b"/v1/chat/completions", "query_string": b"", "headers": [],
                       "app": application}, receive=receive)
    route = next(route for route in application.routes if route.path == "/v1/{path:path}")
    async with application.router.lifespan_context(application):
        response = await route.endpoint(request)
        assert stream.yielded == 0
        iterator = response.body_iterator.__aiter__()
        assert await iterator.__anext__() == b"data: first\n\n"
        assert stream.yielded == 1
        assert not stream.closed
        assert await iterator.__anext__() == b"data: [DONE]\n\n"
        with pytest.raises(StopAsyncIteration):
            await iterator.__anext__()
        await response.background()
    assert stream.closed


def test_gzip_download_preserves_wire_encoding(public, monkeypatch):
    payload = b"archive content" * 50
    stream = UpstreamChunks([gzip.compress(payload)])
    install_transport(monkeypatch, httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream, headers={"content-encoding": "gzip"}),
    ))
    with TestClient(packaged.create_frontend_app(public, "http://127.0.0.1:8301")) as client:
        response = client.get("/api/export")
    assert response.content == payload
    assert response.headers["content-encoding"] == "gzip"
    assert stream.closed


@pytest.mark.asyncio
async def test_failed_backend_lifespan_stops_frontend_and_releases_ports(public):
    from contextlib import asynccontextmanager
    from fastapi import FastAPI

    @asynccontextmanager
    async def fail_startup(application):
        raise RuntimeError("failed backend startup")
        yield

    backend = packaged.bind_listener("127.0.0.1", 0, "backend")
    frontend = packaged.bind_listener("127.0.0.1", 0, "frontend")
    try:
        with pytest.raises(packaged.RunError, match="Backend failed"):
            await asyncio.wait_for(packaged.serve_applications(
                FastAPI(lifespan=fail_startup),
                packaged.create_frontend_app(public, packaged.listener_url(backend)),
                backend, frontend,
            ), timeout=10)
    finally:
        backend.close()
        frontend.close()
