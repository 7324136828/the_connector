"""Persistent skill storage and native skill registry tests."""

import importlib
import json
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.services.agent_service import AgentService
from backend.app.services.skill_manager import SkillManager
from backend.app.services.router import RouteResult


client = TestClient(main.app)
agent_service_module = importlib.import_module("backend.app.services.agent_service")
skill_manager_module = importlib.import_module("backend.app.services.skill_manager")
python_environment_module = importlib.import_module(
    "backend.app.services.python_environment_manager"
)


def skill_payload(name="greet_user"):
    return {
        "name": name,
        "description": "Greet a user by name.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        "type": "python",
        "python_code": "def run(args):\n    return 'Hello, ' + args['name']",
        "source_conversation": "User: Please make a reusable greeting action.",
    }


def test_native_skills_are_registered():
    assert [tool.name for tool in main.agent_service.list_tools()] == [
        "run_python_script",
        "install_python_package",
        "create_coding_skill_from_conversation",
    ]


def test_agent_python_environment_is_created_at_configured_temp_path(monkeypatch, tmp_path):
    environment_dir = tmp_path / "the_connector_python"
    python_executable = python_environment_module.python_executable_for(environment_dir)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        python_executable.parent.mkdir(parents=True)
        python_executable.touch()
        return subprocess.CompletedProcess(command, 0, "", "")

    manager = python_environment_module.PythonEnvironmentManager(
        tmp_path / "environments.db",
        environment_dir,
        tmp_path / "managed",
    )
    monkeypatch.setattr(python_environment_module.subprocess, "run", fake_run)

    assert manager.current_python_executable() == str(python_executable)
    assert manager.current_python_executable() == str(python_executable)
    assert len(calls) == 1
    assert calls[0][0] == [sys.executable, "-m", "venv", str(environment_dir)]


def test_python_runner_uses_dedicated_environment_interpreter(monkeypatch):
    calls = []
    monkeypatch.setattr(
        agent_service_module,
        "_agent_python_executable",
        lambda _environment_manager=None: "agent-python",
    )

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "42\n", "")

    monkeypatch.setattr(agent_service_module.subprocess, "run", fake_run)

    assert agent_service_module.tool_run_python_script("print(42)") == "42"
    assert calls[0][0] == ["agent-python", "-I", "-c", "print(42)"]


def test_persisted_skill_uses_selected_environment_interpreter(monkeypatch, tmp_path):
    skills = SkillManager(tmp_path / "skills.db")
    skills.create_skill(**skill_payload("selected_environment_skill"))

    class SelectedEnvironment:
        @staticmethod
        def current_python_executable():
            return "selected-environment-python"

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"result": "Hello, Ada", "stdout": ""}), "",
        )

    monkeypatch.setattr(skill_manager_module.subprocess, "run", fake_run)
    service = AgentService(skills, SelectedEnvironment())

    result = service.execute_tool("selected_environment_skill", {"name": "Ada"})

    assert result.success is True
    assert result.result == "Hello, Ada"
    assert calls[0][0][0] == "selected-environment-python"
    assert calls[0][0][1:3] == ["-I", "-c"]


def test_package_install_skill_skips_an_available_import(monkeypatch):
    monkeypatch.setattr(
        agent_service_module,
        "_agent_python_executable",
        lambda _environment_manager=None: "agent-python",
    )
    monkeypatch.setattr(
        agent_service_module,
        "_module_available",
        lambda python_executable, import_name: python_executable == "agent-python" and import_name == "bs4",
    )
    monkeypatch.setattr(
        agent_service_module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("pip must not run for an available import"),
    )

    result = main.agent_service.execute_tool(
        "install_python_package",
        {"package": "beautifulsoup4", "import_name": "bs4"},
    )

    assert result.success is True
    assert result.result["status"] == "already_available"


def test_package_install_skill_uses_active_interpreter_and_verifies_import(monkeypatch):
    availability = iter((False, True))
    monkeypatch.setattr(
        agent_service_module,
        "_agent_python_executable",
        lambda _environment_manager=None: "agent-python",
    )
    monkeypatch.setattr(
        agent_service_module,
        "_module_available",
        lambda _python_executable, _import_name: next(availability),
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "Installed beautifulsoup4", "")

    monkeypatch.setattr(agent_service_module.subprocess, "run", fake_run)

    result = main.agent_service.execute_tool(
        "install_python_package",
        {"package": "beautifulsoup4", "import_name": "bs4", "version": "4.12.3"},
    )

    assert result.success is True
    assert result.result["status"] == "installed"
    assert calls[0][0][:4] == ["agent-python", "-m", "pip", "install"]
    assert calls[0][0][-1] == "beautifulsoup4==4.12.3"
    assert calls[0][1]["timeout"] == 180.0


def test_package_install_skill_rejects_pip_flags():
    invalid = main.agent_service.execute_tool(
        "install_python_package",
        {"package": "--user", "import_name": "anything"},
    )
    assert invalid.success is False
    assert "one PyPI distribution name" in invalid.error


def test_created_skill_is_persisted_callable_and_reloaded():
    created = client.post("/api/skills", json=skill_payload())
    assert created.status_code == 201, created.text
    record = created.json()

    with sqlite3.connect(main.skill_manager.db_path) as conn:
        stored = conn.execute(
            "SELECT name, python_code, source_conversation FROM skills WHERE id = ?", (record["id"],),
        ).fetchone()
    assert stored == (
        "greet_user",
        skill_payload()["python_code"],
        skill_payload()["source_conversation"],
    )

    executed = client.post(
        "/api/agent/step", json={"tool": "greet_user", "arguments": {"name": "Ada"}},
    )
    assert executed.status_code == 200
    assert executed.json()["result"] == "Hello, Ada"

    restarted = AgentService(SkillManager(main.skill_manager.db_path))
    assert "greet_user" in [tool.name for tool in restarted.list_tools()]
    assert restarted.execute_tool("greet_user", {"name": "Grace"}).result == "Hello, Grace"


def test_existing_skill_table_migrates_type_to_python(tmp_path):
    database = tmp_path / "legacy-skills.db"
    with sqlite3.connect(database) as conn:
        conn.execute("""
            CREATE TABLE skills (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                python_code TEXT NOT NULL,
                source_conversation TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO skills VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-id", "legacy_python", "Legacy skill", '{"type":"object"}',
                "def run(args):\n    return args", "legacy", "now", "now",
            ),
        )

    manager = SkillManager(database)

    assert manager.get_skill("legacy-id")["type"] == "python"


def test_skill_type_is_detected_when_omitted():
    payload = skill_payload("detected_python")
    payload.pop("type")

    response = client.post("/api/skills", json=payload)

    assert response.status_code == 201, response.text
    assert response.json()["type"] == "python"


def test_cmd_and_cpp_skills_dispatch_to_their_runtimes(monkeypatch, tmp_path):
    skills = SkillManager(tmp_path / "typed-skills.db")
    skills.create_skill(
        name="cmd_echo",
        description="Echo JSON from CMD.",
        parameters={"type": "object"},
        python_code='@echo off\necho {"runtime":"cmd"}',
        source_conversation="cmd",
        type="cmd",
    )
    skills.create_skill(
        name="cpp_echo",
        description="Echo JSON from C++.",
        parameters={"type": "object"},
        python_code="int main() { return 0; }",
        source_conversation="cpp",
        type="c++",
    )

    class SelectedEnvironment:
        @staticmethod
        def current_python_executable():
            return str(tmp_path / "venv" / "Scripts" / "python.exe")

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "g++.exe":
            return subprocess.CompletedProcess(command, 0, "", "")
        runtime = "cmd" if command[0] == "cmd.exe" else "c++"
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"runtime": runtime}), "",
        )

    monkeypatch.setenv("COMSPEC", "cmd.exe")
    monkeypatch.setattr(skill_manager_module, "discover_cpp_compiler", lambda: ("g++", "g++.exe"))
    monkeypatch.setattr(skill_manager_module.subprocess, "run", fake_run)
    service = AgentService(skills, SelectedEnvironment())

    assert service.execute_tool("cmd_echo", {}).result == {"runtime": "cmd"}
    assert service.execute_tool("cpp_echo", {}).result == {"runtime": "c++"}
    assert calls[0][0][:4] == ["cmd.exe", "/d", "/s", "/c"]
    assert calls[1][0][0] == "g++.exe"
    assert calls[1][0][1:3] == ["-std=c++17", "-O2"]
    assert calls[2][0][0].endswith("skill.exe")
    assert calls[2][1]["input"] == "{}"


def test_cpp_skill_compiles_with_discovered_local_compiler():
    if skill_manager_module.discover_cpp_compiler() is None:
        pytest.skip("No supported local C++ compiler is installed.")
    result = skill_manager_module.run_skill_code(
        '#include <iostream>\nint main() { std::cout << "{\\\"runtime\\\":\\\"c++\\\"}"; }',
        {},
        skill_type="c++",
    )
    assert result == {"runtime": "c++"}


def test_native_creation_tool_persists_and_registers_skill():
    response = client.post(
        "/api/agent/step",
        json={"tool": "create_coding_skill_from_conversation", "arguments": skill_payload("welcome_user")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    assert main.skill_manager.get_skill_by_name("welcome_user") is not None
    assert "welcome_user" in [tool.name for tool in main.agent_service.list_tools()]


def test_agent_creation_action_preserves_json_escaped_python_regex(monkeypatch):
    generated = skill_payload("normalize_spaces")
    generated["python_code"] = (
        "import re\n\n"
        "def run(args):\n"
        '    return re.sub(r"\\s+", " ", args["text"]).strip()'
    )
    generated["source_conversation"] = "Normalize whitespace in Japanese lyrics such as 歌詞."
    outputs = iter((
        json.dumps({
            "thought": "Save the reusable skill.",
            "action": {
                "tool": "create_coding_skill_from_conversation",
                "arguments": generated,
            },
            "final_answer": None,
        }, ensure_ascii=False),
        json.dumps({
            "thought": "The skill was saved.",
            "action": None,
            "final_answer": "Saved normalize_spaces.",
        }),
    ))
    monkeypatch.setattr(main.router, "route_chat", lambda **kwargs: RouteResult(
        next(outputs), "mock", "mock-assistant", {"total_tokens": 1}, 1, {},
    ))
    session_id = client.post(
        "/api/sessions", json={
            "config": {
                "sequences": [{"provider": "mock", "model": "mock-assistant", "retries": 0}],
            },
            "user_session": True,
        },
    ).json()["session_id"]

    response = client.post(
        "/api/agent/run", json={"session_id": session_id, "prompt": "Save the skill."},
    )

    assert response.status_code == 200, response.text
    assert response.json()["final_answer"] == "Saved normalize_spaces."
    assert response.json()["steps"][0]["tool"] == "create_coding_skill_from_conversation"
    assert main.skill_manager.get_skill_by_name("normalize_spaces") is not None
    assert main.agent_service.execute_tool(
        "normalize_spaces", {"text": "  a   b  "},
    ).result == "a b"


def test_conversation_endpoint_generates_and_persists_skill(monkeypatch):
    session = client.post(
        "/api/sessions",
        json={
            "config": {"sequences": [{"provider": "mock", "model": "mock-assistant", "retries": 0}]},
            "user_session": True,
        },
    ).json()
    generated = skill_payload("model_chosen_name")
    generated.pop("source_conversation")
    monkeypatch.setattr(main.router, "route_chat", lambda **kwargs: RouteResult(
        json.dumps(generated), "mock", "mock-assistant", {"total_tokens": 10}, 1, {},
    ))

    response = client.post("/api/skills/from-conversation", json={
        "session_id": session["session_id"],
        "name": "requested_name",
        "conversation": "User: Create a greeting skill. Assistant: It should greet a supplied name.",
    })

    assert response.status_code == 201, response.text
    assert response.json()["name"] == "requested_name"
    assert response.json()["source_conversation"].startswith("User: Create")
    assert main.skill_manager.get_skill_by_name("requested_name") is not None


def test_skills_export_and_import_round_trip():
    python_skill = client.post("/api/skills", json=skill_payload("portable_python"))
    assert python_skill.status_code == 201, python_skill.text
    cmd_payload = {
        "name": "portable_cmd",
        "description": "Portable CMD skill.",
        "parameters": {"type": "object"},
        "python_code": "@echo off\necho done",
        "type": "cmd",
        "source_conversation": "Create a CMD skill.",
    }
    cmd_skill = client.post("/api/skills", json=cmd_payload)
    assert cmd_skill.status_code == 201, cmd_skill.text

    exported = client.get("/api/skills/export")
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-disposition"] == 'attachment; filename="skills.json"'
    document = exported.json()
    assert document["version"] == 1
    assert {skill["type"] for skill in document["skills"]} == {"python", "cmd"}
    assert all("id" not in skill and "created_at" not in skill for skill in document["skills"])

    for record in (python_skill.json(), cmd_skill.json()):
        assert client.delete(f"/api/skills/{record['id']}").status_code == 200

    imported = client.post("/api/skills/import", json={**document, "conflict": "error"})
    assert imported.status_code == 200, imported.text
    assert imported.json()["created"] == 2
    assert imported.json()["replaced"] == imported.json()["skipped"] == 0
    assert {skill["name"] for skill in client.get("/api/skills").json()} == {
        "portable_python", "portable_cmd",
    }

    skipped = client.post("/api/skills/import", json={**document, "conflict": "skip"})
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["skipped"] == 2


def test_skill_validation_duplicate_and_delete():
    invalid = skill_payload("Bad Name")
    assert client.post("/api/skills", json=invalid).status_code == 422

    reserved = client.post("/api/skills", json=skill_payload("install_python_package"))
    assert reserved.status_code == 422
    assert "reserved for a native skill" in reserved.json()["detail"]

    created = client.post("/api/skills", json=skill_payload()).json()
    duplicate = client.post("/api/skills", json=skill_payload())
    assert duplicate.status_code == 409

    deleted = client.delete(f"/api/skills/{created['id']}")
    assert deleted.status_code == 200
    missing = client.post(
        "/api/agent/step", json={"tool": "greet_user", "arguments": {"name": "Ada"}},
    ).json()
    assert missing["success"] is False


def test_skill_can_be_edited_renamed_and_reloaded():
    first = client.post("/api/skills", json=skill_payload()).json()
    updated = client.patch(f"/api/skills/{first['id']}", json={
        "name": "enthusiastic_greeting",
        "description": "Greet a user enthusiastically.",
        "python_code": "def run(args):\n    return 'Hello, ' + args['name'] + '!'",
    })

    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "enthusiastic_greeting"
    assert updated.json()["parameters"] == skill_payload()["parameters"]
    assert main.agent_service.execute_tool("greet_user", {"name": "Ada"}).success is False
    result = main.agent_service.execute_tool("enthusiastic_greeting", {"name": "Ada"})
    assert result.success is True
    assert result.result == "Hello, Ada!"

    restarted = AgentService(SkillManager(main.skill_manager.db_path))
    assert restarted.execute_tool("enthusiastic_greeting", {"name": "Grace"}).result == "Hello, Grace!"


def test_skill_edit_validation_preserves_existing_record():
    first = client.post("/api/skills", json=skill_payload()).json()
    invalid = client.patch(f"/api/skills/{first['id']}", json={
        "python_code": "print('missing run function')",
    })
    assert invalid.status_code == 422
    assert main.skill_manager.get_skill(first["id"])["python_code"] == skill_payload()["python_code"]
    assert client.patch(f"/api/skills/{first['id']}", json={}).status_code == 422
    assert client.patch("/api/skills/missing", json={"description": "Changed"}).status_code == 404
