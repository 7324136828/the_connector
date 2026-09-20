"""Validated immutable configuration history snapshots."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .configuration import normalize_config

HistorySource = Literal["upload", "editor", "session", "library", "history"]


class ConfigurationHistoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: dict[str, Any]
    name: str = Field(default="config.json", min_length=1, max_length=200)
    source: HistorySource = "editor"

    @field_validator("config")
    @classmethod
    def validate_config(cls, value):
        return normalize_config(value)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Configuration name must not be blank.")
        return value


class ConfigurationHistoryRecord(BaseModel):
    id: str
    config: dict[str, Any]
    name: str
    source: HistorySource
    first_loaded_at: str
    last_loaded_at: str
    load_count: int
