"""Schemas for persistent typed coding skills."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SkillCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2, max_length=64)
    description: str = Field(..., min_length=1, max_length=2_000)
    parameters: dict[str, Any]
    python_code: str = Field(..., min_length=1, max_length=100_000)
    type: Literal["python", "cmd", "c++"] | None = None
    source_conversation: str = Field(default="", max_length=1_000_000)


class SkillRecord(SkillCreate):
    id: str
    created_at: str
    updated_at: str


class SkillUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=64)
    description: str | None = Field(default=None, min_length=1, max_length=2_000)
    parameters: dict[str, Any] | None = None
    python_code: str | None = Field(default=None, min_length=1, max_length=100_000)
    type: Literal["python", "cmd", "c++"] | None = None
    source_conversation: str | None = Field(default=None, max_length=1_000_000)

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("Provide at least one skill field to update.")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Updated skill fields cannot be null.")
        return self


class SkillFromConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1)
    conversation: str = Field(..., min_length=1, max_length=1_000_000)
    name: str | None = Field(default=None, min_length=2, max_length=64)
    type: Literal["python", "cmd", "c++"] | None = None


class SkillImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    skills: list[SkillCreate]
    conflict: Literal["error", "skip", "replace"] = "error"


class SkillImportResponse(BaseModel):
    created: int
    replaced: int
    skipped: int
    skills: list[SkillRecord]


class SkillDeleteResponse(BaseModel):
    id: str
    name: str
    status: str = "deleted"
