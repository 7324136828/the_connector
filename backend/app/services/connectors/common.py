"""Shared chat models, token tracking, error hierarchy, and retry helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TokenUsage:
    """Actual provider counters for one request; None triggers token estimation."""
    input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens or 0) + (self.output_tokens or 0)


_token_usage: ContextVar[TokenUsage | None] = ContextVar("token_usage", default=None)


@contextmanager
def capture_token_usage() -> Iterator[TokenUsage]:
    """Collect a request's counters without changing connector return values."""
    usage = TokenUsage()
    context_token = _token_usage.set(usage)
    try:
        yield usage
    finally:
        _token_usage.reset(context_token)


def record_token_usage(
    input_tokens: int | None = None, output_tokens: int | None = None
) -> None:
    """Save valid provider counts in the active request capture, if any."""
    usage = _token_usage.get()
    if usage is None:
        return
    if isinstance(input_tokens, int) and not isinstance(input_tokens, bool) and input_tokens >= 0:
        usage.input_tokens = input_tokens
    if isinstance(output_tokens, int) and not isinstance(output_tokens, bool) and output_tokens >= 0:
        usage.output_tokens = output_tokens


class ChatAPIError(RuntimeError):
    """Base error for chat connector failures."""
    def __init__(self, message: str, *, retryable: bool = False, provider: str = "") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.provider = provider


# Backwards compatibility with original-project exception naming
TranslationAPIError = ChatAPIError


def is_retryable_sdk_error(exc: Exception) -> bool:
    """Recognize temporary failures from OpenAI, Anthropic, Gemini and HTTP transports."""
    status_code = getattr(exc, "status_code", None)
    if status_code in {408, 409, 429} or (isinstance(status_code, int) and status_code >= 500):
        return True
    name = type(exc).__name__
    if name in {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError", "ServiceUnavailableError"}:
        return True
    # HTTP transport errors (httpx, urllib, socket)
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    return False


def estimate_tokens(text: str | None) -> int:
    """Estimate token count based on whitespace, word boundaries and CJK characters."""
    if not text:
        return 0
    # ~4 characters per token average in English, or ~1.5 chars per token for CJK/code
    # Simple robust rule: word count * 1.3 or length / 3.8
    words = len(re.findall(r"\w+", text))
    char_estimate = max(1, len(text) // 4)
    return max(words, char_estimate)


def format_messages_to_single_prompt(messages: List[Dict[str, str]], system_prompt: str = "") -> str:
    """Format structured messages into a clean multi-turn prompt when needed."""
    parts: List[str] = []
    if system_prompt:
        parts.append(f"System: {system_prompt.strip()}\n")
    for msg in messages:
        role = msg.get("role", "user").capitalize()
        content = msg.get("content", "").strip()
        parts.append(f"{role}: {content}")
    return "\n\n".join(parts)
