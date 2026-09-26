"""Managed Python environment API and execution routing tests."""

import importlib
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.services.python_environment_manager import PythonEnvironmentManager


client = TestClient(main.app)
agent_service_module = importlib.import_module("backend.app.services.agent_service")
environment_module = importlib.import_module(
    "backend.app.services.python_environment_manager"
)


def fake_venv_creation(command, **_kwargs):
    environment_dir = Path(command[-1])
    executable = environment_module.python_executable_for(environment_dir)
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.touch()
    return subprocess.CompletedProcess(command, 0, "", "")


def test_environment_api_lists_creates_selects_and_persists(monkeypatch):
    monkeypatch.setattr(environment_module.subprocess, "run", fake_venv_creation)

    initial = client.get("/api/python-environments")
    assert initial.status_code == 200
    assert [(item["id"], item["selected"], item["ready"]) for item in initial.json()] == [
        ("default", True, False),
    ]

    created_response = client.post(
        "/api/python-environments",
        json={"name": "Data Science", "select": True},
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()
    assert created["name"] == "Data Science"
    assert created["selected"] is True
    assert created["ready"] is True
    assert Path(created["path"]).parent == main.python_environment_manager.managed_environments_dir

    listed = client.get("/api/python-environments").json()
    assert [item["name"] for item in listed] == ["Default", "Data Science"]
    assert [item["selected"] for item in listed] == [False, True]

    selected_default = client.post("/api/python-environments/default/select")
    assert selected_default.status_code == 200
    assert selected_default.json()["selected"] is True
    assert selected_default.json()["ready"] is True

    restarted = PythonEnvironmentManager(
        main.python_environment_manager.db_path,
        main.python_environment_manager.default_environment_dir,
        main.python_environment_manager.managed_environments_dir,
    )
    assert restarted.selected_environment()["id"] == "default"


def test_environment_api_validates_duplicates_and_missing_ids(monkeypatch):
    monkeypatch.setattr(environment_module.subprocess, "run", fake_venv_creation)

    assert client.post(
        "/api/python-environments", json={"name": "Research"}
    ).status_code == 201
    duplicate = client.post("/api/python-environments", json={"name": "research"})
    assert duplicate.status_code == 409
    invalid = client.post("/api/python-environments", json={"name": "../outside"})
    assert invalid.status_code == 422
    assert client.post("/api/python-environments/missing/select").status_code == 404


def test_python_tool_uses_the_environment_selected_through_the_api(monkeypatch):
    monkeypatch.setattr(environment_module.subprocess, "run", fake_venv_creation)
    created = client.post(
        "/api/python-environments", json={"name": "Agent Work", "select": True}
    ).json()
    expected_python = str(environment_module.python_executable_for(Path(created["path"])))
    calls = []

    def fake_python_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "selected environment\n", "")

    monkeypatch.setattr(agent_service_module.subprocess, "run", fake_python_run)
    response = client.post(
        "/api/agent/step",
        json={"tool": "run_python_script", "arguments": {"code": "print('selected environment')"}},
    )

    assert response.status_code == 200
    assert response.json()["result"] == "selected environment"
    assert calls[0][0] == expected_python
