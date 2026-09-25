"""Configuration management for The Connector backend application."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import List
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .storage import prepare_database

# Root directory of the whole repository
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
DATA_DIR = Path(tempfile.gettempdir()) / "the_connector"
DEFAULT_DB_PATH = DATA_DIR / "connector.db"
LEGACY_DB_PATH = BACKEND_DIR / "data" / "connector.db"
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.json"


class Settings(BaseSettings):
    """Application runtime configuration."""

    # Server settings
    app_name: str = "The Connector API"
    app_version: str = "1.0.0"
    debug: bool = False
    cors_origins: List[str] = ["*"]
    host: str = "0.0.0.0"
    port: int = 8301

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
    kokoro_base_url: str = "http://127.0.0.1:8302"
    kokoro_voice: str = "af_heart"
    kokoro_language: str = "a"
    kokoro_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    kokoro_timeout: float = Field(default=120.0, gt=0)

    # Storage paths
    data_dir: Path = DATA_DIR
    db_path: Path = DEFAULT_DB_PATH

    # Response diagnostics live in system temp, independently of DB_PATH.
    response_logging_enabled: bool = True
    response_log_dir: Path = DATA_DIR / "logs"
    response_log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    response_log_backup_count: int = Field(default=5, ge=1, le=100)
    response_log_max_body_bytes: int = Field(default=0, ge=0)
    request_log_max_body_bytes: int = Field(default=0, ge=0)
    internal_audit_enabled: bool = True

    @model_validator(mode="after")
    def resolve_database_path(self):
        # DATA_DIR remains a supported override; an explicit DB_PATH wins.
        if "db_path" not in self.model_fields_set:
            self.db_path = self.data_dir / "connector.db"
        return self

    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
prepare_database(
    settings.db_path,
    legacy_path=LEGACY_DB_PATH if settings.db_path.resolve() == DEFAULT_DB_PATH.resolve() else None,
)
