"""Chat and Session Pydantic schemas."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from .configuration import normalize_config


class ChatMessage(BaseModel):
    """Single chat message within a session."""
    id: Optional[str] = None
    role: str = Field(..., description="Role of author: user, assistant, system")
    content: str = Field(..., description="Message text content")
    provider: Optional[str] = None
    model: Optional[str] = None
    tokens: Optional[Dict[str, Optional[int]]] = None
    latency_ms: Optional[float] = None
    created_at: Optional[str] = None


class NewSessionRequest(BaseModel):
    """Create a session exclusively from uploaded config.json contents."""
    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = Field(default="New Chat", max_length=200)
    config: Optional[Dict[str, Any]] = None
    config_id: Optional[str] = Field(default=None, min_length=1)
    history_id: Optional[str] = Field(default=None, min_length=1)
    user_session: bool = False

    @field_validator("config")
    @classmethod
    def validate_config(cls, value):
        return normalize_config(value) if value is not None else None

    @model_validator(mode="after")
    def require_configuration(self):
        if sum(value is not None for value in (self.config, self.config_id, self.history_id)) != 1:
            raise ValueError("Provide exactly one of config, config_id, or history_id.")
        return self


class UpdateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: Dict[str, Any]

    @field_validator("config")
    @classmethod
    def validate_config(cls, value):
        return normalize_config(value)


class NewSessionResponse(BaseModel):
    """Response returned upon establishing a new chat session."""
    session_id: str
    title: str
    provider: str
    model: str
    system_prompt: str
    past_memory: bool
    context_window: int
    user_session: bool
    created_at: str
    status: str = "active"


class ChatRequest(BaseModel):
    """The saved config is the only source of routing and memory settings."""
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)


class TokenUsageInfo(BaseModel):
    """Token usage breakdown for a request."""
    input_tokens: Optional[int] = 0
    output_tokens: Optional[int] = 0
    total_tokens: Optional[int] = 0


class ChatResponse(BaseModel):
    """Assistant reply to a chat message."""
    session_id: str
    message_id: str
    role: str = "assistant"
    content: str
    provider: str
    model: str
    tokens: TokenUsageInfo
    latency_ms: float
    attempt_info: Optional[Dict[str, Any]] = None
    created_at: str


class CloseSessionRequest(BaseModel):
    """Request to close or delete a session."""
    session_id: str


class CloseSessionResponse(BaseModel):
    """Response when a session is closed."""
    session_id: str
    status: str
    detail: str


class SessionSummary(BaseModel):
    """Summary of a chat session."""
    session_id: str
    title: str
    provider: str
    model: str
    past_memory: bool
    context_window: int
    user_session: bool
    message_count: int
    created_at: str
    updated_at: str
    status: str


class SessionDetail(BaseModel):
    """Complete session with message history."""
    session: SessionSummary
    messages: List[ChatMessage]
    config: Optional[Dict[str, Any]] = None
    system_prompt: str = ""


# Agent Schemas
class AgentToolDefinition(BaseModel):
    """Agent tool metadata and schema."""
    name: str
    description: str
    parameters: Dict[str, Any]


class AgentRegisterToolRequest(BaseModel):
    """Request to register a custom tool/plugin."""
    name: str
    description: str
    parameters: Dict[str, Any]
    endpoint: Optional[str] = None


class AgentStep(BaseModel):
    """Single reasoning or tool execution step in an agent run."""
    step: int
    thought: str
    tool: Optional[str] = None
    arguments: Optional[Dict[str, Any]] = None
    observation: Optional[str] = None


class AgentRunRequest(BaseModel):
    """An agent run uses its session's configuration and saved memory."""
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    max_steps: int = Field(default=5, ge=1, le=15)
    tools: Optional[List[str]] = None


class AgentRunResponse(BaseModel):
    """Complete output of an agent run."""
    session_id: Optional[str] = None
    prompt: str
    steps: List[AgentStep]
    final_answer: str
    provider: str
    model: str
    total_tokens: int
    latency_ms: float


class AgentStepRequest(BaseModel):
    """Execute a single tool call directly."""
    tool: str
    arguments: Dict[str, Any]


class AgentStepResponse(BaseModel):
    """Direct result of a single tool execution."""
    tool: str
    arguments: Dict[str, Any]
    result: Any
    success: bool
    error: Optional[str] = None


class ModelInfo(BaseModel):
    """Information about a supported LLM model."""
    id: str
    name: str
    provider: str
    context_length: Optional[int] = None
    description: Optional[str] = None
    effort_levels: List[str] = Field(default_factory=list)
    default_effort: Optional[str] = None
