"""Configuration reuse survives edits, inactive models, closes, and restarts."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.schemas.chat import NewSessionRequest
from backend.app.services.configuration_history import ConfigurationHistoryManager
from backend.app.services.configuration_manager import ConfigurationManager
from backend.app.services.session_manager import SessionManager


def config(model="mock-assistant"):
    return {"sequences": [{"provider": "mock", "model": model}]}


@pytest.fixture
def client():
    with TestClient(main.app) as test_client:
        yield test_client


def test_immediate_load_deduplicates_normalized_configs_and_downloads(client):
    assert client.get("/api/config-history").json() == []
    first = client.post("/api/config-history", json={
        "config": config(), "name": " personal.json ", "source": "upload",
    })
    assert first.status_code == 201
    record = first.json()
    assert record["name"] == "personal.json"
    assert record["source"] == "upload"
    assert record["load_count"] == 1
    assert record["config"]["past_memory"] is True
    # Key order, omitted defaults and provider aliases do not duplicate history.
    normalized = dict(reversed(list(json.loads(json.dumps(record["config"])).items())))
    normalized["sequences"][0]["provider"] = "demo"
    second = client.post("/api/config-history", json={
        "config": normalized, "name": "Different label", "source": "history",
    }).json()
    assert second["id"] == record["id"]
    assert second["config"] == record["config"]
    assert second["name"] == record["name"]
    assert second["source"] == record["source"]
    assert second["load_count"] == 2
    assert second["first_loaded_at"] == record["first_loaded_at"]
    assert second["last_loaded_at"] >= record["last_loaded_at"]
    assert client.get("/api/sessions").json() == []
    assert client.get("/api/models").json()["data"] == []
    assert client.get("/api/config-history").json() == [second]
    assert client.get(f"/api/config-history/{record['id']}").json() == second
    download = client.get(f"/api/config-history/{record['id']}/download")
    assert download.status_code == 200
    assert download.json() == record["config"]
    assert download.headers["content-disposition"] == 'attachment; filename="config.json"'


@pytest.mark.parametrize("payload", [
    {"config": {}}, {"config": []}, {"config": config(), "source": "invalid"},
    {"config": config(), "name": " "}, {"config": config(), "extra": True},
])
def test_invalid_loads_do_not_enter_history(client, payload):
    assert client.post("/api/config-history", json=payload).status_code == 422
    assert client.get("/api/config-history").json() == []


def test_validation_does_not_record_history(client):
    assert client.post("/api/config/validate", json=config()).status_code == 200
    assert client.get("/api/config-history").json() == []


def test_blank_session_title_does_not_reject_valid_configuration(client):
    assert client.post("/api/sessions", json={"config": config(), "title": " "}).status_code == 201
    assert client.get("/api/config-history").json()[0]["name"] == "New Chat"


def test_history_session_survives_library_edits_deactivation_and_deletion(client):
    library = client.post("/api/configs", json={
        "name": "My route", "model_id": "route", "config": config(),
    }).json()
    old = client.get("/api/config-history").json()[0]
    path = f"/api/configs/{library['id']}"
    assert client.patch(path, json={"config": config("replacement"), "active": False}).status_code == 200
    assert len(client.get("/api/config-history").json()) == 2
    assert client.post("/api/sessions", json={"config_id": library["id"]}).status_code == 409
    assert client.delete(path).status_code == 204
    reused = client.post("/api/sessions", json={"history_id": old["id"], "title": "Reused"})
    assert reused.status_code == 201
    detail = client.get(f"/api/sessions/{reused.json()['session_id']}").json()
    assert detail["config"] == old["config"]
    assert detail["session"]["title"] == "Reused"
    assert client.get("/api/models").json()["data"] == []
    assert client.get(f"/api/config-history/{old['id']}").json()["load_count"] == 2


def test_session_changes_keep_prior_snapshots_and_closed_sessions(client):
    session = client.post("/api/sessions", json={"config": config(), "title": "First"}).json()
    first = client.get("/api/config-history").json()[0]
    path = f"/api/sessions/{session['session_id']}"
    assert client.patch(path, json={"config": config("second")}).status_code == 200
    history = client.get("/api/config-history").json()
    assert len(history) == 2
    assert history[0]["config"]["sequences"][0]["model"] == "second"
    assert history[1] == first
    assert client.request("DELETE", "/api/close", json={"session_id": session["session_id"]}).status_code == 200
    assert client.patch(path, json={"config": config("third")}).status_code == 409
    assert client.get("/api/config-history").json() == history
    assert client.post("/api/sessions", json={"history_id": first["id"]}).status_code == 201


@pytest.mark.parametrize("payload", [
    {}, {"history_id": "x", "config": config()},
    {"history_id": "x", "config_id": "y"},
    {"history_id": "x", "config_id": "y", "config": config()},
    {"history_id": ""},
])
def test_session_requires_exactly_one_configuration_reference(client, payload):
    assert client.post("/api/sessions", json=payload).status_code == 422


def test_missing_history_is_not_a_fallback(client):
    assert client.get("/api/config-history/missing").status_code == 404
    assert client.get("/api/config-history/missing/download").status_code == 404
    assert client.post("/api/sessions", json={"history_id": "missing"}).status_code == 404
    assert client.get("/api/sessions").json() == []


def test_backfill_active_closed_and_legacy_sessions_once(tmp_path):
    path = tmp_path / "legacy.db"
    sessions = SessionManager(path)
    library = ConfigurationManager(path)
    active = sessions.create_session(NewSessionRequest(config=config("active")))
    closed = sessions.create_session(NewSessionRequest(config=config("closed")))
    sessions.close_session(closed.session_id)
    legacy = sessions.create_session(NewSessionRequest(config=config("old-format")))
    broken = sessions.create_session(NewSessionRequest(config=config("broken")))
    library.create_config("Inactive", "inactive", config("library"), active=False)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM configuration_history")
        conn.execute("DELETE FROM configuration_history_references")
        conn.execute(
            "UPDATE sessions SET provider = 'mock', model = 'legacy-direct', config_json = NULL WHERE id = ?",
            (legacy.session_id,),
        )
        conn.execute("UPDATE sessions SET config_json = '{broken' WHERE id = ?", (broken.session_id,))
    imported = ConfigurationHistoryManager(path).list_history()
    assert len(imported) == 4
    assert {entry["config"]["sequences"][0]["model"] for entry in imported} == {
        "active", "closed", "legacy-direct", "library",
    }
    assert all(entry["load_count"] == 1 for entry in imported)
    # Reinitializing every service and lazily migrating a session cannot duplicate imports.
    reopened = SessionManager(path)
    assert reopened.get_session(legacy.session_id).config["sequences"][0]["model"] == "legacy-direct"
    ConfigurationManager(path)
    assert ConfigurationHistoryManager(path).list_history() == imported
    assert reopened.get_session(active.session_id) is not None


def test_library_metadata_edits_and_rejected_mutations_do_not_count_as_loads(client):
    created = client.post("/api/configs", json={
        "name": "Original", "model_id": "original", "config": config(),
    }).json()
    before = client.get("/api/config-history").json()
    path = f"/api/configs/{created['id']}"
    assert client.patch(path, json={"name": "Renamed", "active": False}).status_code == 200
    assert client.patch(path, json={"config": {}}).status_code == 422
    assert client.post("/api/configs", json={
        "name": "Duplicate", "model_id": "original", "config": config("different"),
    }).status_code == 409
    assert client.get("/api/config-history").json() == before


def test_concurrent_duplicate_loads_are_atomic(tmp_path):
    manager = ConfigurationHistoryManager(tmp_path / "history.db")
    with ThreadPoolExecutor(max_workers=4) as workers:
        records = list(workers.map(lambda _: manager.remember(config()), range(12)))
    assert len({record["id"] for record in records}) == 1
    assert manager.list_history()[0]["load_count"] == 12


def test_history_and_session_writes_rollback_together(tmp_path, monkeypatch):
    manager = SessionManager(tmp_path / "atomic.db")

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("Simulated history write failure")

    monkeypatch.setattr(manager.configuration_history, "remember", fail)
    with pytest.raises(sqlite3.OperationalError):
        manager.create_session(NewSessionRequest(config=config()))
    assert manager.list_sessions() == []


def test_preserves_previous_legacy_snapshot_before_overwrite(tmp_path):
    path = tmp_path / "overwrite.db"
    manager = SessionManager(path)
    session = manager.create_session(NewSessionRequest(config=config("before")))
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM configuration_history")
        conn.execute("DELETE FROM configuration_history_references")
    manager.update_session_config(session.session_id, config("after"))
    records = manager.configuration_history.list_history()
    assert {record["config"]["sequences"][0]["model"] for record in records} == {"before", "after"}
    assert all(record["load_count"] == 1 for record in records)
