"""Backend launcher flags reach the environment before uvicorn imports the app."""

import os
import sys
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import run_backend


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
