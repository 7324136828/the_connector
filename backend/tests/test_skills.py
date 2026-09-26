"""Persistent skill storage and native skill registry tests."""

import json
import sqlite3

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.services.agent_service import AgentService
from backend.app.services.skill_manager import SkillManager
from backend.app.services.router import RouteResult


client = TestClient(main.app)


def skill_payload(name="greet_user"):
    return {
        "name": name,
        "description": "Greet a user by name.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        "python_code": "def run(args):\n    return 'Hello, ' + args['name']",
        "source_conversation": "User: Please make a reusable greeting action.",
    }


def test_only_two_native_skills_are_registered():
    assert [tool.name for tool in main.agent_service.list_tools()] == [
        "run_python_script",
        "create_skill_from_conversation",
    ]


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


def test_native_creation_tool_persists_and_registers_skill():
    response = client.post(
        "/api/agent/step",
        json={"tool": "create_skill_from_conversation", "arguments": skill_payload("welcome_user")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    assert main.skill_manager.get_skill_by_name("welcome_user") is not None
    assert "welcome_user" in [tool.name for tool in main.agent_service.list_tools()]


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


def test_skill_validation_duplicate_and_delete():
    invalid = skill_payload("Bad Name")
    assert client.post("/api/skills", json=invalid).status_code == 422

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
