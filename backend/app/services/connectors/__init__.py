"""Connectors package initialization."""

from .common import (
    ChatAPIError,
    TokenUsage,
    capture_token_usage,
    record_token_usage,
    is_retryable_sdk_error,
    estimate_tokens,
)
from .openai_connector import (
    DEFAULT_OPENAI_MODEL,
    OpenAIAPIError,
    create_openai_client,
    openai_chat,
)
from .claude_connector import (
    DEFAULT_CLAUDE_MODEL,
    ClaudeAPIError,
    create_claude_client,
    claude_chat,
)
from .gemini_connector import (
    DEFAULT_GEMINI_MODEL,
    GeminiAPIError,
    create_gemini_client,
    gemini_chat,
)
from .ollama_connector import (
    DEFAULT_OLLAMA_HOST,
    DEFAULT_OLLAMA_MODEL,
    OllamaError,
    list_ollama_models,
    ollama_chat,
)
from .openrouter_connector import (
    DEFAULT_OPENROUTER_MODEL,
    OpenRouterAPIError,
    create_openrouter_client,
    openrouter_chat,
)
from .mock_connector import mock_chat

__all__ = [
    "ChatAPIError",
    "TokenUsage",
    "capture_token_usage",
    "record_token_usage",
    "is_retryable_sdk_error",
    "estimate_tokens",
    "DEFAULT_OPENAI_MODEL",
    "OpenAIAPIError",
    "create_openai_client",
    "openai_chat",
    "DEFAULT_CLAUDE_MODEL",
    "ClaudeAPIError",
    "create_claude_client",
    "claude_chat",
    "DEFAULT_GEMINI_MODEL",
    "GeminiAPIError",
    "create_gemini_client",
    "gemini_chat",
    "DEFAULT_OLLAMA_HOST",
    "DEFAULT_OLLAMA_MODEL",
    "OllamaError",
    "list_ollama_models",
    "ollama_chat",
    "DEFAULT_OPENROUTER_MODEL",
    "OpenRouterAPIError",
    "create_openrouter_client",
    "openrouter_chat",
    "mock_chat",
]
