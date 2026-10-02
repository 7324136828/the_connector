"""Keep tests isolated from the user's persisted conversations and providers."""
import os
import tempfile

_database_dir = tempfile.TemporaryDirectory(prefix="connector-tests-")
os.environ["DB_PATH"] = os.path.join(_database_dir.name, "bootstrap.db")
os.environ["DEBUG"] = "false"
os.environ["RESPONSE_LOG_DIR"] = os.path.join(_database_dir.name, "response-logs")
os.environ["RESPONSE_LOGGING_ENABLED"] = "true"
os.environ["RESPONSE_LOG_MAX_BYTES"] = "10485760"
os.environ["RESPONSE_LOG_BACKUP_COUNT"] = "5"
os.environ["RESPONSE_LOG_MAX_BODY_BYTES"] = "0"
os.environ["REQUEST_LOG_MAX_BODY_BYTES"] = "0"
os.environ["INTERNAL_AUDIT_ENABLED"] = "true"
os.environ["INTERNAL_AUDIT_MAX_BYTES"] = "10485760"
os.environ["INTERNAL_AUDIT_MAX_ENTRIES"] = "5"

import pytest
from backend.app import main
from backend.app.services.session_manager import SessionManager
from backend.app.services.configuration_manager import ConfigurationManager
from backend.app.services.skill_manager import SkillManager
from backend.app.services.agent_service import AgentService
from backend.app.services.python_environment_manager import PythonEnvironmentManager
from backend.app.services.plugin_manager import PluginManager
from backend.app.api import configurations, compatibility, configuration_history


@pytest.fixture(autouse=True)
def isolate_api_database(monkeypatch, tmp_path, request):
    monkeypatch.setattr(main.response_log_writer, "directory", tmp_path / "response-logs")
    manager = SessionManager(tmp_path / "sessions.db")
    monkeypatch.setattr(main.internal_audit_store, "db_path", manager.db_path)
    monkeypatch.setattr(main, "session_manager", manager)
    monkeypatch.setattr(compatibility, "session_manager", manager)
    monkeypatch.setattr(main, "list_ollama_models", lambda *args: [])
    library = ConfigurationManager(tmp_path / "sessions.db")
    skills = SkillManager(tmp_path / "sessions.db")
    python_environments = PythonEnvironmentManager(
        tmp_path / "sessions.db",
        tmp_path / "the_connector_python",
        tmp_path / "the_connector_python_envs",
    )
    monkeypatch.setattr(main, "skill_manager", skills)
    monkeypatch.setattr(main, "python_environment_manager", python_environments)
    monkeypatch.setattr(main, "agent_service", AgentService(skills, python_environments, manager))
    plugins = PluginManager(tmp_path / "sessions.db", tmp_path / "plugins", lambda: main.agent_service)
    monkeypatch.setattr(main, "plugin_manager", plugins)
    request.addfinalizer(plugins.shutdown)
    for module in (main, configurations, compatibility):
        monkeypatch.setattr(module, "configuration_manager", library)
    for module in (main, configuration_history):
        monkeypatch.setattr(module, "configuration_history_manager", manager.configuration_history)
    return manager
