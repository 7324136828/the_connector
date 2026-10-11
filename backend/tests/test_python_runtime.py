from pathlib import Path

import pytest

from backend.app import python_runtime


def test_development_uses_current_interpreter(monkeypatch):
    monkeypatch.delenv('CONNECTOR_PYTHON_EXECUTABLE', raising=False)
    monkeypatch.setattr(python_runtime.sys, 'frozen', False, raising=False)
    assert python_runtime.runtime_python_executable() == python_runtime.sys.executable


def test_frozen_runtime_uses_external_python(monkeypatch, tmp_path):
    external = tmp_path / 'python.exe'
    external.touch()
    monkeypatch.setattr(python_runtime.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(python_runtime.sys, 'executable', str(tmp_path / 'server.exe'))
    monkeypatch.setenv('CONNECTOR_PYTHON_EXECUTABLE', str(external))
    assert Path(python_runtime.runtime_python_executable()) == external.resolve()
    monkeypatch.delenv('CONNECTOR_PYTHON_EXECUTABLE')
    monkeypatch.setattr(python_runtime.shutil, 'which', lambda name: str(external) if name == 'python3' else None)
    assert python_runtime.runtime_python_executable() == str(external)


def test_frozen_runtime_never_launches_server_as_python(monkeypatch, tmp_path):
    server = tmp_path / 'server.exe'
    server.touch()
    monkeypatch.setattr(python_runtime.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(python_runtime.sys, 'executable', str(server))
    monkeypatch.setenv('CONNECTOR_PYTHON_EXECUTABLE', str(server))
    with pytest.raises(python_runtime.PythonRuntimeError, match='not server.exe'):
        python_runtime.runtime_python_executable()
    monkeypatch.delenv('CONNECTOR_PYTHON_EXECUTABLE')
    monkeypatch.setattr(python_runtime.shutil, 'which', lambda name: str(server))
    with pytest.raises(python_runtime.PythonRuntimeError, match='CONNECTOR_PYTHON_EXECUTABLE'):
        python_runtime.runtime_python_executable()
