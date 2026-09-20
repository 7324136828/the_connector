"""Config library persistence and HTTP contracts, with no provider calls."""

import uuid
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api import configurations
from backend.app.services.configuration_manager import (
    ConfigurationManager,
    ConfigurationNotFoundError,
    DuplicateModelIdError,
)


def config(model="demo"):
    return {"sequences": [{"provider": "mock", "model": model}]}


@pytest.fixture
def library(tmp_path):
    return ConfigurationManager(tmp_path / "library.db")


@pytest.fixture
def client(library, monkeypatch):
    monkeypatch.setattr(configurations, "configuration_manager", library)
    application = FastAPI()
    application.include_router(configurations.router)
    with TestClient(application) as test_client:
        yield test_client


def test_empty_then_persistent_normalized_library(library):
    assert library.list_configs() == []
    original = config()
    record = library.create_config("  Personal model  ", "personal-model", original)
    assert str(uuid.UUID(record["id"])) == record["id"]
    assert record["name"] == "Personal model"
    assert record["active"] is True
    assert record["config"]["past_memory"] is True
    assert "past_memory" not in original
    persisted = ConfigurationManager(library.db_path)
    assert persisted.get_config(record["id"]) == record
    assert persisted.get_active_model("personal-model") == record
    assert persisted.list_configs(active_only=True) == [record]


def test_updates_activation_and_deletion(library):
    first = library.create_config("First", "first", config())
    second = library.create_config("Second", "second", config())
    updated = library.update_config(first["id"], name="Renamed", active=False, description="Saved", config=config("new"))
    assert updated["model_id"] == "first"
    assert updated["id"] == first["id"]
    assert updated["created_at"] == first["created_at"]
    assert updated["config"]["sequences"][0]["model"] == "new"
    assert library.get_active_model("first") is None
    assert library.list_configs(active_only=True) == [second]
    assert len(library.list_configs()) == 2
    assert library.update_config(first["id"]) == updated
    with pytest.raises(DuplicateModelIdError):
        library.create_config("Duplicate inactive ID", "first", config())
    library.update_config(first["id"], active=True)
    assert library.get_active_model("first") is not None
    assert library.delete_config(first["id"]) is True
    assert library.delete_config(first["id"]) is False
    assert library.get_config(first["id"]) is None
    with pytest.raises(ConfigurationNotFoundError):
        library.update_config(first["id"], name="Gone")


@pytest.mark.parametrize("model_id", ["", "bad name", "../bad", "_bad", "bad\n", "a" * 101])
def test_invalid_model_ids(library, model_id):
    with pytest.raises(ValueError):
        library.create_config("Name", model_id, config())


@pytest.mark.parametrize("changes", [
    {"model_id": "replacement"}, {"name": " "}, {"active": "false"},
    {"active": None}, {"description": None}, {"config": None},
    {"config": {"sequences": []}}, {"unknown": True},
])
def test_invalid_updates_do_not_change_record(library, changes):
    record = library.create_config("Original", "original", config())
    with pytest.raises(ValueError):
        library.update_config(record["id"], **changes)
    assert library.get_config(record["id"]) == record


def test_http_crud_and_inactive_download(client):
    assert client.get("/api/configs").json() == []
    payload = {"name": "Library entry", "model_id": "my-model", "config": config()}
    created = client.post("/api/configs", json=payload)
    assert created.status_code == 201
    record = created.json()
    path = f"/api/configs/{record['id']}"
    assert client.get(path).json() == record
    assert client.post("/api/configs", json=payload).status_code == 409
    changed = client.patch(path, json={"active": False, "name": "Inactive"})
    assert changed.status_code == 200
    assert changed.json()["model_id"] == "my-model"
    assert client.get("/api/configs?active_only=true").json() == []
    assert len(client.get("/api/configs").json()) == 1
    download = client.get(path + "/download")
    assert download.status_code == 200
    assert download.headers["content-disposition"] == 'attachment; filename="config.json"'
    assert download.json() == record["config"]
    assert client.patch(path, json={"description": "Still editable"}).status_code == 200
    assert client.delete(path).status_code == 204
    for response in [client.get(path), client.get(path + "/download"), client.patch(path, json={"name": "Missing"}), client.delete(path)]:
        assert response.status_code == 404


def test_http_validation_and_sql_values(client, library):
    payload = {"name": "SQL ' ; DROP TABLE configurations; --", "model_id": "safe", "config": config()}
    record = client.post("/api/configs", json=payload).json()
    assert record["name"] == payload["name"]
    assert library.get_config("' OR 1=1 --") is None
    assert library.get_active_model("' OR 1=1 --") is None
    assert library.delete_config("' OR 1=1 --") is False
    path = f"/api/configs/{record['id']}"
    assert client.patch(path, json={"model_id": "changed"}).status_code == 422
    for invalid in [{"active": "false"}, {"name": " "}, {"config": {}}, {"extra": 1}, {"model_id": "bad/id"}]:
        assert client.post("/api/configs", json={**payload, **invalid}).status_code == 422
    assert library.get_config(record["id"])["model_id"] == "safe"


def test_nullable_context_length(client):
    payload = {"name": "Context", "model_id": "context", "config": config(), "context_length": 32768}
    created = client.post("/api/configs", json=payload)
    assert created.status_code == 201
    assert created.json()["context_length"] == 32768
    path = f"/api/configs/{created.json()['id']}"
    assert client.patch(path, json={"context_length": 16384}).json()["context_length"] == 16384
    assert client.patch(path, json={"context_length": None}).json()["context_length"] is None
    for value in [0, -1, "32000", True, 2.5]:
        assert client.patch(path, json={"context_length": value}).status_code == 422
        assert client.post("/api/configs", json={**payload, "context_length": value}).status_code == 422


def test_migrates_existing_library_table(tmp_path):
    path = tmp_path / "older-library.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE configurations (
                id TEXT PRIMARY KEY, model_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
                config_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
    manager = ConfigurationManager(path)
    record = manager.create_config("Migrated", "migrated", config(), context_length=4096)
    assert record["context_length"] == 4096
    assert ConfigurationManager(path).get_config(record["id"]) == record
