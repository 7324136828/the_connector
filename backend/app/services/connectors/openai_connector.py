"""OpenAI chat connector supporting Chat Completions and Responses APIs."""

from __future__ import annotations

import os
from ...model_capabilities import resolve_effort, default_effort
from typing import Any, Dict, List, Optional

from .common import (
    ChatAPIError,
    is_retryable_sdk_error,
    record_token_usage,
)

DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


class OpenAIAPIError(ChatAPIError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message, retryable=retryable, provider="openai")


def lowest_reasoning_effort(model: str) -> str | None:
    """Detect minimum reasoning effort for reasoning models."""
    return default_effort("openai", model)


def create_openai_client(api_key: Optional[str] = None, timeout: float = 60.0) -> Any:
    """Create an OpenAI SDK client using environment or provided key."""
    key = (api_key or os.environ.get("OPENAI_API_KEY", "")).strip()
    if not key:
        raise OpenAIAPIError("Set OPENAI_API_KEY to use OpenAI.")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise OpenAIAPIError(
            "OpenAI support requires the 'openai' package. Install it with "
            "'pip install openai'."
        ) from exc

    try:
        return OpenAI(api_key=key, timeout=timeout, max_retries=0)
    except Exception as exc:
        raise OpenAIAPIError(f"Could not initialize the OpenAI client: {exc}") from exc


def openai_chat(
    client: Any,
    *,
    model: str,
    messages: List[Dict[str, str]],
    system_prompt: str = "",
    max_output_tokens: int = -1,
    temperature: float = 0.7,
    effort: Optional[str] = None,
) -> str:
    """Send a multi-turn chat request through OpenAI."""
    formatted_messages: List[Dict[str, str]] = []
    if system_prompt:
        formatted_messages.append({"role": "system", "content": system_prompt})
    for m in messages:
        formatted_messages.append({"role": m["role"], "content": m["content"]})

    request_options: Dict[str, Any] = {
        "model": model,
        "messages": formatted_messages,
    }

    effort = resolve_effort("openai", model, effort)
    if effort is not None:
        request_options["reasoning_effort"] = effort
    else:
        request_options["temperature"] = temperature

    if max_output_tokens != -1:
        request_options["max_completion_tokens" if effort is not None else "max_tokens"] = max_output_tokens

    try:
        chat = getattr(client, "chat")
        response = chat.completions.create(**request_options)
    except Exception as exc:
        # Fallback to responses API if model requires it
        if "responses" in str(exc).lower() and hasattr(client, "responses"):
            try:
                response_options = {"model": model, "input": messages, "instructions": system_prompt}
                if effort is not None:
                    response_options["reasoning"] = {"effort": effort}
                if max_output_tokens > 0:
                    response_options["max_output_tokens"] = max_output_tokens
                resp = client.responses.create(**response_options)
                usage = getattr(resp, "usage", None)
                if usage:
                    record_token_usage(getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None))
                text = getattr(resp, "output_text", "")
                if not isinstance(text, str) or not text.strip():
                    raise OpenAIAPIError("OpenAI Responses API returned empty text.")
                return text.strip()
            except Exception as inner:
                raise OpenAIAPIError(f"OpenAI Responses API failed: {inner}", retryable=is_retryable_sdk_error(inner)) from inner

        raise OpenAIAPIError(
            f"OpenAI API request failed: {exc}",
            retryable=is_retryable_sdk_error(exc),
        ) from exc

    usage = getattr(response, "usage", None)
    if usage:
        record_token_usage(
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
        )

    try:
        reply = response.choices[0].message.content
    except (IndexError, AttributeError) as exc:
        raise OpenAIAPIError(f"OpenAI returned an unexpected response structure: {exc}") from exc

    if not isinstance(reply, str) or not reply.strip():
        raise OpenAIAPIError("OpenAI returned an empty response.")

    return reply.strip()
