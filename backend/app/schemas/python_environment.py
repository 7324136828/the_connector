"""Schemas for managed agent Python environments."""

from pydantic import BaseModel, ConfigDict, Field


class PythonEnvironmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=64)
    select: bool = True


class PythonEnvironmentRecord(BaseModel):
    id: str
    name: str
    path: str
    is_default: bool
    selected: bool
    ready: bool
    created_at: str
