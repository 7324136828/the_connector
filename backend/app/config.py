"""Configuration management for The Connector backend application."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .storage import prepare_database
from .user_settings import default_data_directory, initialize_port_settings, resolve_data_directory, settings_file_path

# Repository root, or the installed folder selected by the portable launcher.
ROOT_DIR = Path(os.environ.get("CONNECTOR_RUNTIME_ROOT") or Path(__file__).resolve().parent.parent.parent).resolve()
BACKEND_DIR = ROOT_DIR / "backend"
DATA_DIR = default_data_directory()
DEFAULT_DB_PATH = DATA_DIR / "connector.db"
LEGACY_DB_PATH = BACKEND_DIR / "data" / "connector.db"
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.json"


class Settings(BaseSettings):
    """Application runtime configuration."""

    # Server settings
    app_name: str = "The Connector API"
    app_version: str = "1.0.0"
    debug: bool = Field(default=False, validation_alias="CONNECTOR_DEBUG")
    cors_origins: List[str] = ["*"]
    host: str = "0.0.0.0"
    port: int = 8130

    # Provider API Keys
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    google_api_key: str = ""
    openrouter_api_key: str = ""
    ollama_host: str = "http://localhost:11434"

    # Default model / routing configuration
    default_provider: str = "openai"
    default_model: str = "gpt-4o-mini"
    default_context_window: int = 10
    default_timeout: float = 60.0
    config_path: Path = DEFAULT_CONFIG_PATH

    # Isolated Kokoro text-to-speech service
    kokoro_base_url: str = "http://127.0.0.1:8131"
    kokoro_voice: str = "af_heart"
    kokoro_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    kokoro_timeout: float = Field(default=120.0, gt=0)

    @field_validator("kokoro_voice")
    @classmethod
    def validate_kokoro_voice(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("KOKORO_VOICE must be a non-empty voice actor ID")
        return value.strip()

    # Storage paths
    data_dir: Path = DATA_DIR
    db_path: Path = DEFAULT_DB_PATH
    agent_python_env_dir: Path = DATA_DIR / "python"
    agent_python_envs_dir: Path = DATA_DIR / "python_envs"
    plugin_dir: Path = DATA_DIR / "plugins"
    jobs_dir: Path = DATA_DIR / "jobs"
    settings_path: Path = Field(
        default_factory=settings_file_path, exclude=True,
        validation_alias=AliasChoices("CONNECTOR_SETTINGS_PATH", "SETTINGS_PATH", "settings_path"),
    )

    # Response diagnostics follow the data folder, independently of DB_PATH.
    completion_no_database_access: bool = False
    response_logging_enabled: bool = True
    response_log_dir: Path = DATA_DIR / "logs"
    response_log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    response_log_backup_count: int = Field(default=5, ge=1, le=100)
    response_log_max_body_bytes: int = Field(default=0, ge=0)
    request_log_max_body_bytes: int = Field(default=0, ge=0)
    internal_audit_enabled: bool = True
    internal_audit_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    internal_audit_max_entries: int = Field(default=5, ge=1, le=1000)

    @model_validator(mode="after")
    def resolve_storage_paths(self):
        preferences = initialize_port_settings(self.settings_path)
        self.port = preferences["connector_port"]
        if "kokoro_base_url" not in self.model_fields_set:
            self.kokoro_base_url = f"http://127.0.0.1:{preferences['kokoro_port']}"
        # Environment and constructor overrides take precedence over user JSON.
        if "data_dir" not in self.model_fields_set:
            self.data_dir = resolve_data_directory(preferences.get("data_dir", ""))
        else:
            self.data_dir = resolve_data_directory(str(self.data_dir))
        if "db_path" not in self.model_fields_set:
            self.db_path = self.data_dir / "connector.db"
        for field, subdirectory in (
            ("plugin_dir", "plugins"), ("response_log_dir", "logs"),
            ("agent_python_env_dir", "python"), ("agent_python_envs_dir", "python_envs"),
            ("jobs_dir", "jobs"),
        ):
            if field not in self.model_fields_set:
                setattr(self, field, self.data_dir / subdirectory)
        return self

    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )


settings = Settings()
prepare_database(
    settings.db_path,
    legacy_path=LEGACY_DB_PATH if settings.db_path.resolve() == DEFAULT_DB_PATH.resolve() else None,
)
