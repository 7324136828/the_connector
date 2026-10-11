"""Backend launcher flags reach the environment before uvicorn imports the app."""

import os
import sys
import shutil
import json
import socket
import subprocess
from types import SimpleNamespace

import pytest

import run_backend


@pytest.fixture(autouse=True)
def isolate_saved_ports(monkeypatch, tmp_path):
    monkeypatch.setenv("CONNECTOR_SETTINGS_PATH", str(tmp_path / "launcher-settings.json"))


@pytest.mark.parametrize("reload", [False, True])
def test_completion_database_flag_is_inherited_by_backend(monkeypatch, reload):
    monkeypatch.chdir(run_backend.ROOT)
    monkeypatch.setenv("COMPLETION_NO_DATABASE_ACCESS", "false")
    monkeypatch.setattr(sys, "argv", ["run_backend.py", "--completion-no-database-access"]
                        + (["--reload"] if reload else []))
    launches = []

    def launch(app, **kwargs):
        launches.append((app, kwargs, os.environ["COMPLETION_NO_DATABASE_ACCESS"]))

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=launch))
    monkeypatch.setitem(sys.modules, "dotenv", SimpleNamespace(load_dotenv=lambda *args, **kwargs: None))
    run_backend.main()
    assert len(launches) == 1
    app, options, flag = launches[0]
    assert app == "backend.app.main:app" and flag == "true"
    assert options["reload"] is reload


def test_windows_full_stack_accepts_double_dash_flag(tmp_path):
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    # Use the launcher's real parameter block without starting its services.
    probe = tmp_path / "probe.ps1"
    source = (run_backend.ROOT / "run.ps1").read_text(encoding="utf-8")
    probe.write_text(source.split('$ErrorActionPreference = "Stop"', 1)[0]
                     + '\nWrite-Output ("{0},{1},{2}" -f $CompletionNoDatabaseAccess, $Local, $BackendPort)\n',
                     encoding="utf-8")
    result = subprocess.run([powershell, "-NoLogo", "-NoProfile", "-File", str(probe),
                             "-Local", "--completion-no-database-access", "-BackendPort", "8401"],
                            check=True, capture_output=True, text=True, timeout=30)
    assert result.stdout.strip() == "True,True,8401"


def test_backend_busy_port_falls_forward_and_strict_port_rejects_it(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_backend.py", "--local", "--port", "8401"])
    monkeypatch.setattr(run_backend, "available_port", lambda *args, **kwargs: 8402)
    launches = []
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *args, **kwargs: launches.append(kwargs)))
    monkeypatch.setitem(sys.modules, "dotenv", SimpleNamespace(load_dotenv=lambda *args, **kwargs: None))
    run_backend.main()
    assert launches[0]["host"] == "127.0.0.1"
    assert launches[0]["port"] == 8402
    launches.clear()
    monkeypatch.setattr(sys, "argv", ["run_backend.py", "--local", "--port", "8401", "--strict-port"])
    with pytest.raises(SystemExit) as error:
        run_backend.main()
    assert error.value.code == 2
    assert launches == []


def test_backend_reads_saved_port_and_explicit_flag_overrides_it(monkeypatch, tmp_path):
    preferences = {"connector_port": 9200, "react_port": 9201, "kokoro_port": 9202}
    (tmp_path / "launcher-settings.json").write_text(json.dumps(preferences), encoding="utf-8")
    monkeypatch.setenv("BACKEND_PORT", "8301")
    monkeypatch.setenv("CONNECTOR_BACKEND_PORT", "8301")
    monkeypatch.setenv("PORT", "8301")
    monkeypatch.setattr(run_backend, "available_port", lambda port, *args, **kwargs: port)
    monkeypatch.setitem(sys.modules, "dotenv", SimpleNamespace(load_dotenv=lambda *args, **kwargs: None))
    launches = []
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *args, **kwargs: launches.append(kwargs)))
    monkeypatch.setattr(sys, "argv", ["run_backend.py", "--local"])
    run_backend.main()
    assert launches[-1]["port"] == 9200
    monkeypatch.setattr(sys, "argv", ["run_backend.py", "--local", "--port", "9205"])
    run_backend.main()
    assert launches[-1]["port"] == 9205


def test_windows_plan_uses_resolved_ports_urls_and_cors(tmp_path):
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    source = (run_backend.ROOT / "run.ps1").read_text(encoding="utf-8")
    source = source.split("# Put this supervisor", 1)[0]
    root = str(run_backend.ROOT).replace("'", "''")
    source = source.replace("$root = $PSScriptRoot", f"$root = '{root}'")
    source += '\n$plan | ConvertTo-Json -Depth 10 -Compress\n'
    probe = tmp_path / "plan.ps1"
    probe.write_text(source, encoding="utf-8")
    environment = {**os.environ, "CORS_ORIGINS": '["https://example.test"]'}
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        port = busy.getsockname()[1]
        if port > 65532:
            pytest.skip("Ephemeral port is too close to the maximum")
        result = subprocess.run([
            powershell, "-NoLogo", "-NoProfile", "-File", str(probe), "-Local",
            "-BackendPort", str(port), "-UiPort", str(port), "-KokoroPort", str(port),
            "-PythonExecutable", sys.executable, "-NoReload",
        ], check=True, capture_output=True, text=True, env=environment, timeout=30)
    plan = json.loads(result.stdout.splitlines()[-1])
    assert len(set(plan["ports"].values())) == 3
    assert min(plan["ports"].values()) > port
    assert plan["host"] == "127.0.0.1"
    assert plan["urls"]["backend"] == f"http://127.0.0.1:{plan['ports']['backend']}"
    assert plan["lan_urls"]["frontend"] == []
    assert f"http://127.0.0.1:{plan['ports']['frontend']}" in json.loads(plan["cors_origins"])
