"""Full-stack launcher plans stay consistent across platforms and services."""

from __future__ import annotations

import errno
import json
import os
import socket
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

import run


@pytest.fixture(autouse=True)
def clear_launcher_environment(monkeypatch, tmp_path):
    for key in ("BACKEND_HOST", "HOST", "BACKEND_PORT", "PORT", "CONNECTOR_BACKEND_PORT", "FRONTEND_PORT", "KOKORO_PORT", "CORS_ORIGINS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CONNECTOR_SETTINGS_PATH", str(tmp_path / "launcher-settings.json"))


def test_plan_reserves_distinct_ports_and_updates_every_service_url(monkeypatch):
    monkeypatch.setattr(run, "resolve_host", lambda host: (socket.AF_INET, host))
    selections = []

    def select(start, reserved, **kwargs):
        selections.append((start, set(reserved), kwargs))
        return next(port for port in range(start + 1, 65536) if port not in reserved)

    monkeypatch.setattr(run, "available_port", select)
    monkeypatch.setattr(run, "lan_urls_for_host", lambda host, port: [f"http://192.168.1.20:{port}"])
    plan = run.make_plan(run.parse_args([
        "--backend-port", "8301", "--frontend-port", "8301", "--kokoro-port", "8301",
    ]))
    assert plan["ports"] == {"backend": 8302, "frontend": 8303, "kokoro": 8304}
    assert [selected[1] for selected in selections] == [set(), {8302}, {8302, 8303}]
    assert all(selected[2] == {"host": "0.0.0.0", "family": socket.AF_INET} for selected in selections)
    assert plan["urls"]["backend"] == "http://127.0.0.1:8302"
    assert plan["lan_urls"]["frontend"] == ["http://192.168.1.20:8303"]
    environment = run.service_environment(plan)
    assert environment["CONNECTOR_BACKEND_PORT"] == "8302"
    assert environment["CONNECTOR_BACKEND_URL"] == environment["VITE_BACKEND_URL"] == plan["urls"]["backend"]
    assert environment["KOKORO_BASE_URL"] == plan["urls"]["kokoro"]


def test_saved_ports_take_precedence_over_environment_and_cli_can_override(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKEND_HOST", "192.168.1.20")
    monkeypatch.setenv("BACKEND_PORT", "9000")
    monkeypatch.setenv("FRONTEND_PORT", "9001")
    monkeypatch.setenv("KOKORO_PORT", "9002")
    monkeypatch.setattr(run, "resolve_host", lambda host: (socket.AF_INET, host))
    monkeypatch.setattr(run, "available_port", lambda port, *args, **kwargs: port)
    plan = run.make_plan(run.parse_args([]))
    assert plan["host"] == "192.168.1.20"
    assert plan["ports"] == {"backend": 8130, "frontend": 5130, "kokoro": 8131}
    preferences = {"connector_port": 9200, "react_port": 9201, "kokoro_port": 9202, "data_dir": str(tmp_path / "data"), "future": {"custom": True}}
    settings_file = tmp_path / "launcher-settings.json"
    settings_file.write_text(json.dumps(preferences), encoding="utf-8")
    plan = run.make_plan(run.parse_args([]))
    assert plan["ports"] == {"backend": 9200, "frontend": 9201, "kokoro": 9202}
    assert json.loads(settings_file.read_text(encoding="utf-8")) == preferences
    plan = run.make_plan(run.parse_args(["--local", "--backend-port", "9100"]))
    assert plan["host"] == "127.0.0.1"
    assert plan["ports"]["backend"] == 9100
    assert plan["ports"]["frontend"] == 9201
    assert plan["ports"]["kokoro"] == 9202
    assert plan["lan_urls"]["frontend"] == []


def test_launch_initializes_missing_port_fields_preserving_preferences(monkeypatch, tmp_path):
    settings_file = tmp_path / "launcher-settings.json"
    original = {"connector_port": 9200, "data_dir": str(tmp_path / "data"), "other": "preserved"}
    settings_file.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(run, "available_port", lambda port, *args, **kwargs: port)
    plan = run.make_plan(run.parse_args(["--local"]))
    assert plan["requested_ports"] == {"backend": 9200, "frontend": 5130, "kokoro": 8131}
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {**original, "react_port": 5130, "kokoro_port": 8131}


def test_source_launch_initializes_ports_from_custom_project_mapping(monkeypatch, tmp_path):
    runtime_root = tmp_path / "custom project defaults"
    runtime_root.mkdir()
    defaults = {"react_port": 9530, "connector_port": 9531, "kokoro_port": 9532}
    (runtime_root / "port_mapping.json").write_text(json.dumps(defaults), encoding="utf-8")
    monkeypatch.setenv("CONNECTOR_RUNTIME_ROOT", str(runtime_root))
    settings_file = tmp_path / "launcher-settings.json"
    original = {"connector_port": 9631, "data_dir": str(tmp_path / "saved data"), "other": "preserved"}
    settings_file.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(run, "available_port", lambda port, *args, **kwargs: port)
    plan = run.make_plan(run.parse_args(["--local"]))
    assert plan["requested_ports"] == {"backend": 9631, "frontend": 9530, "kokoro": 9532}
    assert plan["urls"]["frontend"] == "http://127.0.0.1:9530"
    assert plan["urls"]["backend"] == "http://127.0.0.1:9631"
    assert plan["urls"]["kokoro"] == "http://127.0.0.1:9532"
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {**original, "react_port": 9530, "kokoro_port": 9532}
    assert json.loads((runtime_root / "port_mapping.json").read_text(encoding="utf-8")) == defaults


@pytest.mark.parametrize("value", [True, 0, 65536, "8130", 8130.5])
def test_launch_rejects_invalid_saved_port_types_and_values(monkeypatch, tmp_path, value):
    settings_file = tmp_path / "launcher-settings.json"
    original = {"connector_port": value}
    settings_file.write_text(json.dumps(original), encoding="utf-8")
    with pytest.raises(run.RunError, match="connector_port"):
        run.make_plan(run.parse_args(["--local"]))
    assert json.loads(settings_file.read_text(encoding="utf-8")) == original


def test_port_probe_skips_busy_and_reserved_ports():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        port = busy.getsockname()[1]
        if port > 65533:
            pytest.skip("Ephemeral port is too close to the maximum")
        selected = run.available_port(port, {port + 1})
        assert selected >= port + 2
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", selected))


def test_port_probe_reports_invalid_bind_without_scanning_all_ports():
    probe = mock.MagicMock()
    probe.__enter__.return_value.bind.side_effect = OSError(errno.EADDRNOTAVAIL, "Unavailable interface")
    with mock.patch.object(run.socket, "socket", return_value=probe):
        with pytest.raises(run.RunError, match="Cannot bind"):
            run.available_port(8301, host="192.0.2.1")
    probe.__enter__.return_value.bind.assert_called_once_with(("192.0.2.1", 8301))


@pytest.mark.parametrize("value", [0, -1, 65536, "invalid"])
def test_invalid_ports_fail_early(value):
    with pytest.raises(run.RunError):
        run.parse_port(value, "backend port")


def test_ipv6_urls_use_brackets():
    assert run.service_url("::", 8301) == "http://[::1]:8301"
    assert run.service_url("2001:db8::1", 8301) == "http://[2001:db8::1]:8301"


def test_restricted_cors_includes_local_and_discovered_lan_ui_origins():
    environment = {"CORS_ORIGINS": '["https://example.test"]'}
    run.merge_cors_origins(environment, 5131, ["http://192.168.1.20:5131"])
    assert json.loads(environment["CORS_ORIGINS"]) == [
        "https://example.test", "http://localhost:5131", "http://127.0.0.1:5131", "http://192.168.1.20:5131",
    ]
    wildcard = {"CORS_ORIGINS": '["*"]'}
    run.merge_cors_origins(wildcard, 5131, [])
    assert wildcard == {"CORS_ORIGINS": '["*"]'}
    default = {}
    run.merge_cors_origins(default, 5131, [])
    assert default == {}


def test_windows_supervisor_receives_python_and_all_flags(monkeypatch):
    monkeypatch.setattr(run, "os", SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setattr(run, "select_runtime", lambda: run.Path(sys.executable))
    monkeypatch.setattr(run, "load_env_file", lambda: None)
    monkeypatch.setattr(sys, "argv", [
        "run.py", "--host", "192.168.1.20", "--backend-port", "8401",
        "--frontend-port", "5230", "--kokoro-port", "8402", "--no-reload",
        "--completion-no-database-access",
    ])
    calls = []
    monkeypatch.setattr(run.subprocess, "call", lambda command, **kwargs: calls.append(command) or 0)
    assert run.main() == 0
    command = calls[0]
    for parameter, value in [
        ("-PythonExecutable", sys.executable), ("-BindHost", "192.168.1.20"),
        ("-BackendPort", "8401"), ("-UiPort", "5230"), ("-KokoroPort", "8402"),
    ]:
        assert command[command.index(parameter) + 1] == value
    assert "-NoReload" in command
    assert "-CompletionNoDatabaseAccess" in command


def test_previous_windows_wrapper_flags_remain_accepted():
    args = run.parse_args(["-Local", "-BackendPort", "8401", "-UiPort", "5230", "-KokoroPort", "8402"])
    assert args.local
    assert (args.backend_port, args.frontend_port, args.kokoro_port) == (8401, 5230, 8402)


def test_reload_flags_are_mutually_exclusive():
    with pytest.raises(SystemExit) as error:
        run.parse_args(["--reload", "--no-reload"])
    assert error.value.code == 2
