"""GET /api/configuration/detail/{name} returns the exact saved config JSON.

Names are URL-encoded display names, including inactive configurations. A missing
name returns 404 and duplicate display names return 409 rather than an arbitrary
configuration. Existing /api/configs/{id} metadata APIs remain available.
"""

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.api import configurations
from backend.app.services.configuration_manager import AmbiguousConfigurationNameError


@pytest.fixture
def client():
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def library():
    return configurations.configuration_manager


def detail_path(name):
    return f"/api/configuration/detail/{quote(name, safe='')}"


def saved_config():
    return {
        "context_window": 10,
        "memory_scope": "all_sessions",
        "memory_window": 20,
        "past_memory": True,
        "gross_max_input_token": 100000,
        "gross_max_output_token": 10000,
        "sequences": [{"model": "demo", "provider": "mock", "retries": 2}],
        "system_prompt": "You are a helpful, precise, and thoughtful AI assistant.",
    }


@pytest.mark.parametrize("name", [
    "Personal configuration", "配置 🧪", "Research/team", "100% ready? #1",
    "SQL ' ; DROP TABLE configurations; --",
])
def test_detail_returns_only_saved_configuration_json(client, library, name):
    record = library.create_config(name, "demo-route", saved_config(), active=False)
    response = client.get(detail_path(name))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == record["config"]
    assert "id" not in response.json()
    assert library.get_config(record["id"]) == record
    # The stable ID API still returns metadata and its nested configuration.
    assert client.get(f"/api/configs/{record['id']}").json() == record


def test_detail_requires_exact_display_name_without_id_or_model_fallback(client, library):
    record = library.create_config("Personal configuration", "demo-route", saved_config())
    for name in ("Missing", "personal configuration", "Personal", " Personal configuration ",
                 record["id"], record["model_id"], "' OR 1=1 --"):
        response = client.get(detail_path(name))
        assert response.status_code == 404
        assert response.json() == {"detail": "Configuration not found."}
    assert library.list_configs() == [record]


def test_duplicate_display_names_are_ambiguous_even_when_one_is_inactive(client, library):
    first = library.create_config("Shared name", "first-route", saved_config())
    second = library.create_config("Shared name", "second-route", saved_config(), active=False)
    with pytest.raises(AmbiguousConfigurationNameError):
        library.get_config_by_name("Shared name")
    response = client.get(detail_path("Shared name"))
    assert response.status_code == 409
    assert response.json() == {"detail": "Multiple saved configurations are named 'Shared name'."}
    assert client.get(f"/api/configs/{first['id']}").json() == first
    assert client.get(f"/api/configs/{second['id']}").json() == second
    library.delete_config(first["id"])
    assert client.get(detail_path("Shared name")).json() == second["config"]


def test_detail_tracks_saved_configuration_rename_and_replacement(client, library):
    record = library.create_config("Original name", "demo-route", saved_config())
    replacement = saved_config()
    replacement["system_prompt"] = "Updated prompt."
    updated = library.update_config(record["id"], name="Updated name", config=replacement)
    assert client.get(detail_path("Original name")).status_code == 404
    assert client.get(detail_path("Updated name")).json() == updated["config"]
