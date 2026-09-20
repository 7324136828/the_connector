"""Validated requests and records for the saved configuration library."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .configuration import normalize_config


class ConfigurationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200, strict=True)
    model_id: str = Field(
        min_length=1, max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$", strict=True,
    )
    description: str = Field(default="", max_length=4000, strict=True)
    active: bool = Field(default=True, strict=True)
    context_length: int | None = Field(default=None, gt=0, strict=True)
    config: dict[str, Any]

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must contain non-whitespace characters.")
        return value.strip()

    @field_validator("config")
    @classmethod
    def validate_config(cls, value: dict) -> dict:
        return normalize_config(value)


class ConfigurationUpdate(BaseModel):
    """Model IDs are stable; config updates replace the complete snapshot."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200, strict=True)
    description: str | None = Field(default=None, max_length=4000, strict=True)
    active: bool | None = Field(default=None, strict=True)
    context_length: int | None = Field(default=None, gt=0, strict=True)
    config: dict[str, Any] | None = None

    @field_validator("name", "description", "active", "config", mode="before")
    @classmethod
    def reject_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Fields cannot be null; omit a field to preserve its value.")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return ConfigurationCreate.validate_name(value)

    @field_validator("config")
    @classmethod
    def validate_config(cls, value: dict) -> dict:
        return normalize_config(value)


class ConfigurationRecord(BaseModel):
    id: str
    model_id: str
    name: str
    description: str
    active: bool
    context_length: int | None = None
    config: dict[str, Any]
    created_at: str
    updated_at: str
