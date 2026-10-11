"""Portable runtime validation and Connector-specific streaming proxy behavior."""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from unittest import mock

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import packaged
from backend import packaged_kokoro


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


@pytest.mark.parametrize("external_url", [None, "https://speech.example:9443"])
def test_main_supervises_bundle_only_without_explicit_speech_url(
    public, clean_runtime_environment, monkeypatch, external_url,
):
    from backend import network

    monkeypatch.setattr(packaged, "runtime_directory", lambda: public.parent)
    backend = mock.Mock(spec=socket.socket)
    frontend = mock.Mock(spec=socket.socket)
    backend.getsockname.return_value = ("127.0.0.1", 8130)
    frontend.getsockname.return_value = ("127.0.0.1", 5130)
    monkeypatch.setattr(packaged, "bind_listener", mock.Mock(side_effect=[backend, frontend]))
    monkeypatch.setattr(network, "lan_urls", lambda listener: [])
    bundle = mock.Mock()
    bundled_speech = mock.Mock(return_value=bundle)
    supervisor = mock.AsyncMock(return_value=0)
    monkeypatch.setattr(packaged, "KokoroProcess", bundled_speech)
    monkeypatch.setattr(packaged, "serve_applications", supervisor)
    if external_url:
        monkeypatch.setenv("KOKORO_BASE_URL", external_url)
    assert packaged.main(["--kokoro-port", "8551"]) == 0
    if external_url:
        bundled_speech.assert_not_called()
        assert supervisor.call_args.kwargs["kokoro"] is None
        assert os.environ["KOKORO_BASE_URL"] == external_url
    else:
        from backend.app.config import settings

        bundled_speech.assert_called_once_with(
            public.parent, 8551, log_directory=settings.response_log_dir,
        )
        assert supervisor.call_args.kwargs["kokoro"] is bundle
        assert os.environ["KOKORO_BASE_URL"] == "http://127.0.0.1:8551"
    backend.close.assert_called_once()
    frontend.close.assert_called_once()


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


def test_bundled_speech_port_must_be_distinct_unless_external_service_is_configured(
    clean_runtime_environment, monkeypatch,
):
    with pytest.raises(SystemExit) as error:
        packaged.parse_args(["--kokoro-port", "8130"])
    assert error.value.code == 2
    monkeypatch.setenv("KOKORO_BASE_URL", "https://speech.example")
    assert packaged.parse_args(["--kokoro-port", "8130"]).kokoro_port == 8130


def test_missing_kokoro_bundle_explains_how_to_restore_it(tmp_path, monkeypatch):
    launch = mock.Mock()
    monkeypatch.setattr(packaged_kokoro.subprocess, "Popen", launch)
    runtime = packaged.KokoroProcess(tmp_path, 8131)
    with pytest.raises(packaged.RunError, match="Bundled Kokoro is missing") as error:
        runtime.start()
    assert "build.bat" in str(error.value)
    assert str(tmp_path / "python-kokoro" / "kokoro.exe") in str(error.value)
    launch.assert_not_called()


@pytest.mark.parametrize("content", [
    None, "{", "[]", "{}", '{"device": "auto"}', '{"device": "mps"}',
    '{"device": null}', '{"device": []}',
])
def test_invalid_kokoro_runtime_profile_cannot_launch(tmp_path, monkeypatch, content):
    executable = tmp_path / "python-kokoro" / "kokoro.exe"
    executable.parent.mkdir()
    executable.touch()
    profile = executable.parent / "runtime.json"
    if content is not None:
        profile.write_text(content, encoding="utf-8")
    launch = mock.Mock()
    monkeypatch.setattr(packaged_kokoro.subprocess, "Popen", launch)
    with pytest.raises(packaged.RunError, match="Kokoro runtime profile") as error:
        packaged.KokoroProcess(tmp_path, 8131).start()
    assert str(profile) in str(error.value)
    assert "build.bat" in str(error.value)
    launch.assert_not_called()


def test_busy_speech_port_is_not_reused_or_killed(tmp_path, monkeypatch):
    executable = tmp_path / "python-kokoro" / "kokoro.exe"
    executable.parent.mkdir()
    executable.touch()
    (executable.parent / "runtime.json").write_text('{"device": "cuda"}', encoding="utf-8")
    launch = mock.Mock()
    monkeypatch.setattr(packaged_kokoro.subprocess, "Popen", launch)
    listener = packaged.bind_listener("127.0.0.1", 0, "occupied speech")
    try:
        runtime = packaged.KokoroProcess(tmp_path, listener.getsockname()[1])
        with pytest.raises(packaged.RunError, match="speech port is already in use") as error:
            runtime.start()
        assert "--kokoro-port" in str(error.value)
        assert "KOKORO_BASE_URL" in str(error.value)
        launch.assert_not_called()
        assert listener.fileno() != -1
    finally:
        listener.close()


@pytest.mark.parametrize("device, environment_device, profile_encoding", [
    ("cuda", "cpu", "utf-8"), ("cuda", "cpu", "utf-8-sig"),
    ("cpu", "cuda", "utf-8"),
])
def test_kokoro_launches_beside_renamed_executable_with_built_device_and_hidden_console(
    tmp_path, monkeypatch, device, environment_device, profile_encoding,
):
    root = tmp_path / "Renamed Connector"
    executable = root / "python-kokoro" / "kokoro.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    (executable.parent / "runtime.json").write_text(
        json.dumps({"device": device}), encoding=profile_encoding,
    )
    listener = packaged.bind_listener("127.0.0.1", 0, "find unused speech port")
    port = listener.getsockname()[1]
    listener.close()
    process = mock.Mock()
    process.poll.return_value = None
    launch = mock.Mock(return_value=process)
    job = mock.Mock()
    monkeypatch.setattr(packaged_kokoro.subprocess, "Popen", launch)
    monkeypatch.setattr(packaged_kokoro, "WindowsProcessJob", mock.Mock(return_value=job))
    monkeypatch.setenv("KOKORO_DEVICE", environment_device)
    runtime = packaged.KokoroProcess(root, port)
    try:
        runtime.start()
        args, options = launch.call_args
        assert args[0] == [str(executable), "--host", "127.0.0.1", "--port", str(port),
                           "--device", device]
        assert options["cwd"] == executable.parent
        assert options["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
        assert options["env"]["KOKORO_DEVICE"] == device
        assert os.environ["KOKORO_DEVICE"] == environment_device
        assert runtime.device == device
        assert options["stdout"] is runtime.log
        assert options["stderr"] == subprocess.STDOUT
        if os.name == "nt":
            assert options["creationflags"] == subprocess.CREATE_NO_WINDOW
            job.assign.assert_called_once_with(process)
        assert runtime.log_path == root / "logs" / "kokoro.log"
    finally:
        runtime.stop()
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=5)
    assert runtime.log is None
    if os.name == "nt":
        job.close.assert_called_once()


def test_independent_speech_environment_removes_backend_runtime_paths(tmp_path, monkeypatch):
    bundle = tmp_path / "backend-bundle"
    tools = tmp_path / "tools"
    monkeypatch.setattr(packaged_kokoro.sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join((str(bundle), str(bundle / "dlls"), str(tools))))
    assert packaged_kokoro.speech_environment()["PATH"] == str(tools)
    assert os.environ["PATH"].startswith(str(bundle))


def test_speech_logs_follow_configured_user_storage_instead_of_install_folder(tmp_path):
    runtime = packaged.KokoroProcess(
        tmp_path / "installed-app", 8131, log_directory=tmp_path / "user-data" / "logs",
    )
    assert runtime.log_path == tmp_path / "user-data" / "logs" / "kokoro.log"


def health_transport(monkeypatch, handler):
    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["timeout"] == 2.0
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(packaged_kokoro.httpx, "AsyncClient", client)


@pytest.mark.parametrize("device", ["cuda", "cpu"])
@pytest.mark.asyncio
async def test_speech_startup_waits_for_identified_python312_health(tmp_path, monkeypatch, device):
    requests = []

    def health(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("starting", request=request)
        return httpx.Response(200, json={"service": "python-kokoro", "status": "ok",
                                         "python": "3.12.10", "device": device})

    health_transport(monkeypatch, health)
    runtime = packaged.KokoroProcess(tmp_path, 8551)
    runtime.device = device
    runtime.process = mock.Mock()
    runtime.process.poll.return_value = None
    assert await runtime.wait_ready(asyncio.Event())
    assert len(requests) == 2
    assert str(requests[-1].url) == "http://127.0.0.1:8551/health"


@pytest.mark.parametrize("device, reported_device", [
    ("cuda", "cpu"), ("cuda", None), ("cpu", "cuda"),
])
@pytest.mark.asyncio
async def test_bundled_speech_cannot_silently_switch_device(
    tmp_path, monkeypatch, device, reported_device,
):
    health_transport(monkeypatch, lambda request: httpx.Response(200, json={
        "service": "python-kokoro", "status": "ok", "python": "3.12.10",
        "device": reported_device,
    }))
    runtime = packaged.KokoroProcess(tmp_path, 8551)
    runtime.device = device
    runtime.process = mock.Mock()
    runtime.process.poll.return_value = None
    with pytest.raises(packaged.RunError, match="this bundle requires") as error:
        await runtime.wait_ready(asyncio.Event())
    assert str(runtime.log_path) in str(error.value)


@pytest.mark.parametrize("health", [
    {"service": "unrelated", "status": "ok", "python": "3.12.10"},
    {"service": "python-kokoro", "status": "ok", "python": "3.14.0"},
    {"service": "python-kokoro", "status": "loading", "python": "3.12.10"},
    [],
])
@pytest.mark.asyncio
async def test_unidentified_speech_health_cannot_open_application(tmp_path, monkeypatch, health):
    health_transport(monkeypatch, lambda request: httpx.Response(200, json=health))
    runtime = packaged.KokoroProcess(tmp_path, 8551)
    runtime.process = mock.Mock()
    runtime.process.poll.return_value = None
    with pytest.raises(packaged.RunError, match="Unexpected speech service"):
        await runtime.wait_ready(asyncio.Event())


@pytest.mark.asyncio
async def test_speech_startup_timeout_and_early_exit_name_diagnostic_log(tmp_path, monkeypatch):
    health_transport(monkeypatch, lambda request: httpx.Response(503))
    runtime = packaged.KokoroProcess(tmp_path, 8551)
    runtime.process = mock.Mock()
    runtime.process.poll.return_value = None
    with pytest.raises(packaged.RunError, match="did not become ready") as error:
        await runtime.wait_ready(asyncio.Event(), timeout=0)
    assert str(runtime.log_path) in str(error.value)
    runtime.process.poll.return_value = 17
    with pytest.raises(packaged.RunError, match="exit code 17") as error:
        await runtime.wait_ready(asyncio.Event())
    assert str(runtime.log_path) in str(error.value)


def fake_application_servers(monkeypatch, on_start=None):
    servers = []

    class FakeServer:
        def __init__(self, config):
            self.should_exit = False
            self.force_exit = False
            self.started = False
            servers.append(self)

        async def serve(self, sockets):
            self.started = True
            if on_start is not None:
                on_start(servers)
            while not self.should_exit:
                await asyncio.sleep(0)

    monkeypatch.setattr(packaged, "ManagedServer", FakeServer)
    return servers


@pytest.mark.asyncio
async def test_speech_startup_failure_prevents_api_frontend_and_cleans_child(monkeypatch):
    servers = fake_application_servers(monkeypatch)
    runtime = mock.Mock()
    runtime.wait_ready = mock.AsyncMock(side_effect=packaged.RunError("speech startup failed"))
    with pytest.raises(packaged.RunError, match="speech startup failed"):
        await packaged.serve_applications(mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(), kokoro=runtime)
    runtime.start.assert_called_once()
    runtime.stop.assert_called_once()
    assert not any(server.started for server in servers)


@pytest.mark.asyncio
async def test_speech_is_verified_before_api_and_child_failure_stops_both(monkeypatch):
    servers = fake_application_servers(monkeypatch)
    runtime = mock.Mock()

    async def ready(stopped):
        assert not any(server.started for server in servers)
        return True

    async def fail_speech():
        assert all(server.started for server in servers)
        raise packaged.RunError("speech process exited")

    runtime.wait_ready = ready
    runtime.monitor = fail_speech
    with pytest.raises(packaged.RunError, match="Kokoro failed: speech process exited"):
        await asyncio.wait_for(packaged.serve_applications(
            mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(), kokoro=runtime,
        ), timeout=5)
    assert all(server.should_exit for server in servers)
    runtime.stop.assert_called_once()


@pytest.mark.asyncio
async def test_ctrl_c_stops_application_and_owned_speech_process(monkeypatch):
    handlers = {}

    def install_handler(signum, handler):
        previous = handlers.get(signum, signal.SIG_DFL)
        handlers[signum] = handler
        return previous

    def interrupt_when_running(servers):
        if all(server.started for server in servers):
            handlers[signal.SIGINT](signal.SIGINT, None)

    monkeypatch.setattr(packaged.signal, "signal", install_handler)
    servers = fake_application_servers(monkeypatch, interrupt_when_running)
    runtime = mock.Mock()
    runtime.wait_ready = mock.AsyncMock(return_value=True)

    async def monitor():
        await asyncio.Event().wait()

    runtime.monitor = monitor
    assert await asyncio.wait_for(packaged.serve_applications(
        mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(), kokoro=runtime,
    ), timeout=5) == 0
    assert all(server.should_exit for server in servers)
    runtime.stop.assert_called_once()
    assert handlers[signal.SIGINT] == signal.SIG_DFL


@pytest.mark.asyncio
async def test_ctrl_c_during_speech_startup_never_opens_api(monkeypatch):
    servers = fake_application_servers(monkeypatch)
    runtime = mock.Mock()

    async def ready(stopped):
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        assert stopped.is_set()
        return False

    runtime.wait_ready = ready
    assert await packaged.serve_applications(
        mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(), kokoro=runtime,
    ) == 0
    assert not any(server.started for server in servers)
    runtime.stop.assert_called_once()


def test_speech_process_is_killed_if_termination_times_out(tmp_path):
    runtime = packaged.KokoroProcess(tmp_path, 8131)
    runtime.process = mock.Mock()
    runtime.process.poll.return_value = None
    runtime.process.wait.side_effect = [subprocess.TimeoutExpired("kokoro", 5), 1]
    runtime.stop()
    runtime.process.terminate.assert_called_once()
    runtime.process.kill.assert_called_once()
    assert runtime.process.wait.call_count == 2


@pytest.mark.skipif(os.name != "nt", reason="Win32 job process ownership")
def test_windows_job_close_stops_owned_child_without_console_signal():
    job = packaged_kokoro.WindowsProcessJob()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        job.assign(process)
        assert process.poll() is None
        job.close()
        # Windows may report exit code zero when a job is closed. Finishing
        # within five seconds proves the sixty-second child was stopped.
        assert process.wait(timeout=5) is not None
    finally:
        job.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
