"""OpenRouter chat connector using OpenAI SDK or REST requests."""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Dict, List, Optional
from ...model_capabilities import resolve_effort

from .common import (
    ChatAPIError,
    is_retryable_sdk_error,
    record_token_usage,
)

DEFAULT_OPENROUTER_MODEL = "openai/gpt-4o-mini"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterAPIError(ChatAPIError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message, retryable=retryable, provider="openrouter")


def create_openrouter_client(api_key: Optional[str] = None, timeout: float = 60.0) -> Any:
    """Initialize client pointing to OpenRouter API."""
    key = (api_key or os.environ.get("OPENROUTER_API_KEY", "")).strip()
    if not key:
        raise OpenRouterAPIError("Set OPENROUTER_API_KEY to use OpenRouter.")
    try:
        from openai import OpenAI
        return OpenAI(api_key=key, base_url=OPENROUTER_BASE_URL, timeout=timeout, max_retries=0)
    except ImportError:
        return None
    except Exception as exc:
        raise OpenRouterAPIError(f"Could not initialize OpenRouter client: {exc}") from exc


def openrouter_chat(
    client: Any,
    *,
    model: str,
    messages: List[Dict[str, str]],
    system_prompt: str = "",
    max_output_tokens: int = -1,
    temperature: float = 0.7,
    timeout: float = 60.0,
    effort: Optional[str] = None,
) -> str:
    """Send chat completions to OpenRouter."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    effort = resolve_effort("openrouter", model, effort)

    formatted_messages: List[Dict[str, str]] = []
    if system_prompt:
        formatted_messages.append({"role": "system", "content": system_prompt})
    for m in messages:
        formatted_messages.append({"role": m["role"], "content": m["content"]})

    # If client is OpenAI SDK instance
    if client is not None:
        options: Dict[str, Any] = {
            "model": model,
            "messages": formatted_messages,
            "temperature": temperature,
        }
        if max_output_tokens > 0:
            options["max_tokens"] = max_output_tokens
        if effort is not None:
            options.pop("temperature", None)
            options["extra_body"] = {"reasoning": {"effort": effort}}

        try:
            resp = getattr(client, "chat").completions.create(**options)
            usage = getattr(resp, "usage", None)
            if usage:
                record_token_usage(getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None))
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            raise OpenRouterAPIError(f"OpenRouter API request failed: {exc}", retryable=is_retryable_sdk_error(exc)) from exc

    # Direct REST fallback
    if not api_key:
        raise OpenRouterAPIError("Set OPENROUTER_API_KEY to use OpenRouter.")

    payload: Dict[str, Any] = {
        "model": model,
        "messages": formatted_messages,
        "temperature": temperature,
    }
    if max_output_tokens > 0:
        payload["max_tokens"] = max_output_tokens
    if effort is not None:
        payload.pop("temperature", None)
        payload["reasoning"] = {"effort": effort}

    req = urllib.request.Request(
        f"{OPENROUTER_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/the_connector",
            "X-Title": "The Connector",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise OpenRouterAPIError(f"OpenRouter REST request failed: {exc}", retryable=True) from exc

    usage = data.get("usage", {})
    record_token_usage(usage.get("prompt_tokens"), usage.get("completion_tokens"))
    choices = data.get("choices", [])
    if not choices:
        raise OpenRouterAPIError("OpenRouter returned no choices.")
    text = choices[0].get("message", {}).get("content", "").strip()
    return text
