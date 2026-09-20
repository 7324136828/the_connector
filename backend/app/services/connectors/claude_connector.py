"""Anthropic Claude chat connector using the Messages API."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from ...model_capabilities import resolve_effort

from .common import (
    ChatAPIError,
    is_retryable_sdk_error,
    record_token_usage,
)

DEFAULT_CLAUDE_MODEL = "claude-3-5-haiku-20241022"
DEFAULT_CLAUDE_OUTPUT_TOKENS = 4096


class ClaudeAPIError(ChatAPIError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message, retryable=retryable, provider="claude")


def create_claude_client(api_key: Optional[str] = None, timeout: float = 60.0) -> Any:
    """Initialize Anthropic client using ANTHROPIC_API_KEY."""
    key = (api_key or os.environ.get("ANTHROPIC_API_KEY", "")).strip()
    if not key:
        raise ClaudeAPIError("Set ANTHROPIC_API_KEY to use Claude.")
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise ClaudeAPIError(
            "Claude support requires the 'anthropic' package. Install it with "
            "'pip install anthropic'."
        ) from exc
    try:
        return Anthropic(api_key=key, timeout=timeout, max_retries=0)
    except Exception as exc:
        raise ClaudeAPIError(f"Could not initialize the Claude client: {exc}") from exc


def claude_chat(
    client: Any,
    *,
    model: str,
    messages: List[Dict[str, str]],
    system_prompt: str = "",
    max_output_tokens: int = -1,
    temperature: float = 0.7,
    effort: Optional[str] = None,
) -> str:
    """Send a multi-turn Messages request to Claude."""
    anthropic_messages: List[Dict[str, str]] = []
    # Anthropic expects alternating user/assistant messages
    for msg in messages:
        role = "assistant" if msg["role"] == "assistant" else "user"
        anthropic_messages.append({"role": role, "content": msg["content"]})

    # If first message is assistant, Claude rejects it. Ensure starting with user.
    if anthropic_messages and anthropic_messages[0]["role"] == "assistant":
        anthropic_messages.insert(0, {"role": "user", "content": "Hello"})

    options: Dict[str, Any] = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": DEFAULT_CLAUDE_OUTPUT_TOKENS if max_output_tokens <= 0 else max_output_tokens,
        "temperature": temperature,
    }

    if system_prompt:
        options["system"] = system_prompt

    effort = resolve_effort("claude", model, effort)
    if effort is not None:
        options["output_config"] = {"effort": effort}

    try:
        response = getattr(client, "messages").create(**options)
    except Exception as exc:
        raise ClaudeAPIError(
            f"Claude API request failed: {exc}",
            retryable=is_retryable_sdk_error(exc),
        ) from exc

    usage = getattr(response, "usage", None)
    if usage:
        input_counts = [getattr(usage, "input_tokens", None)]
        for name in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            count = getattr(usage, name, None)
            if count is not None:
                input_counts.append(count)
        valid_inputs = [c for c in input_counts if isinstance(c, int) and c >= 0]
        total_input = sum(valid_inputs) if valid_inputs else None
        record_token_usage(total_input, getattr(usage, "output_tokens", None))

    content = getattr(response, "content", None)
    if not isinstance(content, list) or not content:
        raise ClaudeAPIError("Claude returned an invalid or empty response.")

    text_parts = [
        block.text for block in content
        if getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str)
    ]
    reply = "".join(text_parts).strip()
    if not reply:
        raise ClaudeAPIError("Claude returned empty text content.")

    return reply
