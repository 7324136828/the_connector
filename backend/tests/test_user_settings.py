"""User preferences persist across installs and take effect on restart."""

import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

from backend.app import main
from backend.app.config import Settings
from backend.app.services.temp_manager import TempManager
from backend.app.user_settings import (
    UserSettingsError, default_data_directory, initialize_port_settings, load_port_mapping,
    load_user_settings, port_mapping_path, resolve_data_directory, save_data_directory,
    settings_file_path,
)


def test_settings_live_in_roaming_appdata(monkeypatch, tmp_path):
    monkeypatch.delenv("CONNECTOR_SETTINGS_PATH")
    if os.name == "nt":
        monkeypatch.setenv("APPDATA", str(tmp_path))
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert settings_file_path() == tmp_path / "the_connector" / "settings.json"
    assert load_user_settings() == {}
    assert json.loads(settings_file_path().read_text(encoding="utf-8")) == {}


def test_storage_setting_controls_all_default_paths(monkeypatch, tmp_path):
    for name in ("DATA_DIR", "DB_PATH", "PLUGIN_DIR", "RESPONSE_LOG_DIR", "AGENT_PYTHON_ENV_DIR", "AGENT_PYTHON_ENVS_DIR", "JOBS_DIR"):
        monkeypatch.delenv(name, raising=False)
    folder = tmp_path / "saved data"
    save_data_directory(str(folder))
    settings = Settings(_env_file=None)
    assert settings.data_dir == folder
    assert settings.db_path == folder / "connector.db"
    assert settings.plugin_dir == folder / "plugins"
    assert settings.response_log_dir == folder / "logs"
    assert settings.agent_python_env_dir == folder / "python"
    assert settings.agent_python_envs_dir == folder / "python_envs"
    assert settings.jobs_dir == folder / "jobs"
    monkeypatch.setattr(main.settings, "jobs_dir", settings.jobs_dir)
    assert TempManager().get_job_dir("example").parent == folder / "jobs"


def test_env_and_dotenv_overrides_win_over_saved_folder(monkeypatch, tmp_path):
    saved = tmp_path / "saved"
    save_data_directory(str(saved))
    env_file = tmp_path / ".env"
    env_file.write_text(f'DATA_DIR="{(tmp_path / "dotenv").as_posix()}"\n', encoding="utf-8")
    monkeypatch.delenv("DATA_DIR")
    monkeypatch.delenv("DB_PATH")
    assert Settings(_env_file=env_file).data_dir == tmp_path / "dotenv"
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "environment"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "custom.db"))
    settings = Settings(_env_file=env_file)
    assert settings.data_dir == tmp_path / "environment"
    assert settings.db_path == tmp_path / "custom.db"
    monkeypatch.setenv("PLUGIN_DIR", str(tmp_path / "custom-plugins"))
    assert Settings(_env_file=None).plugin_dir == tmp_path / "custom-plugins"


def test_empty_env_paths_keep_saved_folder_and_defaults(monkeypatch, tmp_path):
    folder = tmp_path / "saved"
    save_data_directory(str(folder))
    for name in ("DATA_DIR", "DB_PATH", "PLUGIN_DIR", "JOBS_DIR"):
        monkeypatch.setenv(name, "")
    env_file = tmp_path / ".env"
    env_file.write_text("DATA_DIR=\nDB_PATH=\nPLUGIN_DIR=\nJOBS_DIR=\n", encoding="utf-8")
    settings = Settings(_env_file=env_file)
    assert settings.data_dir == folder
    assert settings.db_path == folder / "connector.db"
    assert settings.plugin_dir == folder / "plugins"
    assert settings.jobs_dir == folder / "jobs"


def test_invalid_saved_folder_can_be_overridden_and_repaired(monkeypatch, tmp_path):
    stale = tmp_path / "became-a-file"
    save_data_directory(str(stale))
    stale.write_text("This path is no longer a folder")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "valid-override"))
    assert Settings(_env_file=None).data_dir == tmp_path / "valid-override"
    client = TestClient(main.app)
    response = client.get("/api/settings")
    assert response.status_code == 200
    assert response.json()["configured_data_dir"] == str(stale)
    assert "not a file" in response.json()["configuration_error"]
    replacement = tmp_path / "repaired-folder"
    response = client.patch("/api/settings", json={"data_dir": str(replacement)})
    assert response.status_code == 200
    assert "configuration_error" not in response.json()
    monkeypatch.delenv("DATA_DIR")
    assert Settings(_env_file=None).data_dir == replacement


def test_user_path_expands_host_variables_and_reset_restores_temp(monkeypatch, tmp_path):
    monkeypatch.setenv("CONNECTOR_TEST_HOME", str(tmp_path))
    assert resolve_data_directory("%CONNECTOR_TEST_HOME%/data") == tmp_path / "data"
    assert resolve_data_directory("$CONNECTOR_TEST_HOME/data") == tmp_path / "data"
    save_data_directory(str(tmp_path / "custom"))
    save_data_directory("")
    assert resolve_data_directory(load_user_settings()["data_dir"]) == default_data_directory()


@pytest.mark.parametrize("value", ["relative/folder", "%CONNECTOR_UNDEFINED_TEST_VAR%/data", "a\x00b", None])
def test_invalid_paths_are_rejected_without_overwriting(value, tmp_path):
    original = load_user_settings()
    with pytest.raises(UserSettingsError):
        save_data_directory(value)
    assert load_user_settings() == original


def test_file_target_and_malformed_json_are_preserved(tmp_path):
    file_target = tmp_path / "file"
    file_target.write_text("file")
    with pytest.raises(UserSettingsError, match="not a file"):
        save_data_directory(str(file_target))
    path = settings_file_path()
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(UserSettingsError, match="settings JSON"):
        load_user_settings()
    assert path.read_text(encoding="utf-8") == "{broken"


def test_save_preserves_unrelated_settings_and_has_no_staging_files(tmp_path):
    path = settings_file_path()
    path.write_text(json.dumps({"data_dir": "", "future_preference": {"value": 3}}))
    save_data_directory(str(tmp_path / "new"))
    assert load_user_settings()["future_preference"] == {"value": 3}
    assert not list(path.parent.glob(".settings-*"))


def test_concurrent_first_starts_always_read_complete_settings(tmp_path):
    path = tmp_path / "preferences" / "settings.json"
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda _: load_user_settings(path), range(12)))
    assert results == [{}] * 12


def test_missing_ports_are_persisted_without_overwriting_preferences(tmp_path):
    path = settings_file_path()
    path.write_text(json.dumps({"react_port": 5513, "data_dir": str(tmp_path / "saved"), "custom": True}))
    result = initialize_port_settings()
    assert result == {"react_port": 5513, "connector_port": 8130, "kokoro_port": 8131,
                      "data_dir": str(tmp_path / "saved"), "custom": True}
    assert load_user_settings() == result
    assert initialize_port_settings() == result


def test_empty_settings_populate_all_default_ports():
    assert load_user_settings() == {}
    assert initialize_port_settings() == load_port_mapping()
    assert load_user_settings() == load_port_mapping()


def test_custom_mapping_fills_missing_ports_and_preserves_saved_values(tmp_path):
    mapping = tmp_path / "port_mapping.json"
    defaults = {"react_port": 6000, "connector_port": 6001, "kokoro_port": 6002}
    mapping.write_text(json.dumps(defaults), encoding="utf-8-sig")
    settings_path = tmp_path / "user" / "settings.json"
    assert initialize_port_settings(settings_path, defaults_path=mapping) == defaults
    saved = dict(defaults, data_dir=str(tmp_path / "saved"), custom={"enabled": True})
    saved.pop("connector_port")
    settings_path.write_text(json.dumps(saved), encoding="utf-8")
    changed = {"react_port": 6100, "connector_port": 6101, "kokoro_port": 6102}
    mapping.write_text(json.dumps(changed), encoding="utf-8")
    expected = dict(saved, connector_port=6101)
    assert initialize_port_settings(settings_path, defaults_path=mapping) == expected
    assert load_user_settings(settings_path) == expected
    assert json.loads(mapping.read_text(encoding="utf-8")) == changed


@pytest.mark.parametrize("content", [
    None, "{", "[]", "{}",
    '{"react_port": true, "connector_port": 6001, "kokoro_port": 6002}',
    '{"react_port": "6000", "connector_port": 6001, "kokoro_port": 6002}',
    '{"react_port": 0, "connector_port": 6001, "kokoro_port": 6002}',
    '{"react_port": 65536, "connector_port": 6001, "kokoro_port": 6002}',
    '{"react_port": 6000, "connector_port": 6000, "kokoro_port": 6002}',
    '{"react_port": 6000, "connector_port": 6001, "kokoro_port": 6001}',
])
def test_invalid_mapping_fails_before_creating_or_modifying_settings(tmp_path, content):
    mapping = tmp_path / "port_mapping.json"
    if content is not None:
        mapping.write_text(content, encoding="utf-8")
    preferences = tmp_path / "preferences" / "settings.json"
    with pytest.raises(UserSettingsError) as error:
        initialize_port_settings(preferences, defaults_path=mapping)
    assert str(mapping) in str(error.value)
    assert not preferences.parent.exists()
    preferences.parent.mkdir()
    original = '{"react_port": 6200, "custom": true}'
    preferences.write_text(original, encoding="utf-8")
    with pytest.raises(UserSettingsError):
        initialize_port_settings(preferences, defaults_path=mapping)
    assert preferences.read_text(encoding="utf-8") == original


def test_runtime_mapping_path_survives_cwd_and_portable_folder_rename(monkeypatch, tmp_path):
    monkeypatch.delenv("CONNECTOR_RUNTIME_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert port_mapping_path() == Path(__file__).resolve().parents[2] / "port_mapping.json"
    runtime = tmp_path / "Release"
    runtime.mkdir()
    mapping = {"react_port": 6300, "connector_port": 6301, "kokoro_port": 6302}
    (runtime / "port_mapping.json").write_text(json.dumps(mapping), encoding="utf-8")
    runtime = runtime.rename(tmp_path / "Renamed portable folder")
    monkeypatch.setattr("backend.app.user_settings.sys.frozen", True, raising=False)
    monkeypatch.setattr("backend.app.user_settings.sys.executable", str(runtime / "server.exe"))
    assert port_mapping_path() == runtime / "port_mapping.json"
    assert load_port_mapping() == mapping
    explicit_root = tmp_path / "explicit"
    monkeypatch.setenv("CONNECTOR_RUNTIME_ROOT", str(explicit_root))
    assert port_mapping_path() == explicit_root / "port_mapping.json"


@pytest.mark.parametrize("value", [None, True, "8130", 8130.5, 0, 65536])
def test_invalid_saved_ports_report_error_without_overwriting(value):
    path = settings_file_path()
    original = {"connector_port": value, "custom": True}
    path.write_text(json.dumps(original))
    with pytest.raises(UserSettingsError, match="connector_port"):
        initialize_port_settings()
    assert load_user_settings() == original


def test_saved_ports_configure_backend_and_local_speech(monkeypatch):
    monkeypatch.delenv("KOKORO_BASE_URL", raising=False)
    path = settings_file_path()
    path.write_text(json.dumps({"connector_port": 9010, "kokoro_port": 9011}))
    settings = Settings(_env_file=None)
    assert settings.port == 9010
    assert settings.kokoro_base_url == "http://127.0.0.1:9011"
    monkeypatch.setenv("KOKORO_BASE_URL", "http://speech-server:9999")
    assert Settings(_env_file=None).kokoro_base_url == "http://speech-server:9999"


def test_backend_config_uses_runtime_mapping_for_new_settings(monkeypatch, tmp_path):
    monkeypatch.delenv("KOKORO_BASE_URL", raising=False)
    monkeypatch.setenv("CONNECTOR_RUNTIME_ROOT", str(tmp_path))
    mapping = {"react_port": 6400, "connector_port": 6401, "kokoro_port": 6402}
    (tmp_path / "port_mapping.json").write_text(json.dumps(mapping), encoding="utf-8")
    settings = Settings(_env_file=None)
    assert settings.port == 6401
    assert settings.kokoro_base_url == "http://127.0.0.1:6402"
    assert load_user_settings() == mapping


def test_config_and_launcher_use_the_same_settings_path(monkeypatch, tmp_path):
    preferred = tmp_path / "preferred.json"
    legacy = tmp_path / "legacy.json"
    monkeypatch.setenv("CONNECTOR_SETTINGS_PATH", str(preferred))
    monkeypatch.setenv("SETTINGS_PATH", str(legacy))
    settings = Settings(_env_file=None)
    assert settings.settings_path == settings_file_path() == preferred
    assert preferred.is_file()
    assert not legacy.exists()
    monkeypatch.delenv("CONNECTOR_SETTINGS_PATH")
    assert Settings(_env_file=None).settings_path == settings_file_path() == legacy


def test_failed_save_retains_preferences_and_cleans_staging_file(monkeypatch, tmp_path):
    original = load_user_settings()
    def cannot_publish(*args):
        raise PermissionError("Settings file is locked")
    monkeypatch.setattr("backend.app.user_settings.os.replace", cannot_publish)
    with pytest.raises(PermissionError):
        save_data_directory(str(tmp_path / "new"))
    assert load_user_settings() == original
    assert not list(settings_file_path().parent.glob(".settings-*"))


def test_api_persists_folder_without_switching_running_services(monkeypatch, tmp_path):
    client = TestClient(main.app)
    running = tmp_path / "running"
    monkeypatch.setattr(main.settings, "data_dir", running)
    database_before = main.session_manager.db_path
    response = client.get("/api/settings")
    assert response.status_code == 200
    assert response.json()["settings_path"] == str(settings_file_path())
    destination = tmp_path / "after-restart"
    response = client.patch("/api/settings", json={"data_dir": str(destination)})
    assert response.status_code == 200
    assert response.json()["configured_data_dir"] == str(destination)
    assert response.json()["data_dir"] == str(running)
    assert response.json()["restart_required"] is True
    assert main.settings.data_dir == running
    assert main.session_manager.db_path == database_before
    assert not destination.exists()
    monkeypatch.delenv("DATA_DIR")
    assert Settings(_env_file=None).data_dir == destination
    assert client.patch("/api/settings", json={"data_dir": "relative"}).status_code == 422
    assert client.patch("/api/settings", json={"data_dir": 1}).status_code == 422
    assert load_user_settings()["data_dir"] == str(destination)
    assert client.patch("/api/settings", json={"data_dir": ""}).status_code == 200
    assert load_user_settings()["data_dir"] == ""


def test_runtime_root_resolves_routing_config_and_dotenv_beside_executable(tmp_path):
    runtime = tmp_path / "portable"
    runtime.mkdir()
    (runtime / "port_mapping.json").write_text(json.dumps(load_port_mapping()), encoding="utf-8")
    (runtime / ".env").write_text("DEFAULT_MODEL=portable-model\n", encoding="utf-8")
    env = dict(os.environ, CONNECTOR_RUNTIME_ROOT=str(runtime))
    env.pop("DEFAULT_MODEL", None)
    result = subprocess.run(
        [sys.executable, "-c", "from backend.app.config import settings; print(settings.config_path); print(settings.default_model)"],
        env=env, capture_output=True, text=True, check=True,
    )
    assert result.stdout.splitlines() == [str(runtime / "config.json"), "portable-model"]
