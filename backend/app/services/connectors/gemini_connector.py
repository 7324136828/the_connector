"""Google Gemini chat connector supporting Google GenAI SDK and REST fallback."""

from __future__ import annotations

import json
import math
import os
import urllib.request
from typing import Any, Dict, List, Optional
from ...model_capabilities import resolve_effort, gemini_thinking_config

from .common import (
    ChatAPIError,
    record_token_usage,
)

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


class GeminiAPIError(ChatAPIError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message, retryable=retryable, provider="gemini")


def get_gemini_api_key(explicit_key: Optional[str] = None) -> str:
    key = (
        (explicit_key or "").strip()
        or os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )
    return key


def create_gemini_client(api_key: Optional[str] = None, timeout: float = 60.0) -> Any:
    """Create Google GenAI SDK client or None if SDK not available."""
    key = get_gemini_api_key(api_key)
    if not key:
        raise GeminiAPIError("Set GEMINI_API_KEY (or GOOGLE_API_KEY) to use Gemini.")

    try:
        from google import genai
        return genai.Client(
            api_key=key,
            vertexai=False,
            http_options={
                "timeout": max(1, math.ceil(timeout * 1000)),
                "retry_options": {"attempts": 1},
            },
        )
    except ImportError:
        # We can still use direct REST fallback
        return None
    except Exception as exc:
        raise GeminiAPIError(f"Could not initialize Gemini client: {exc}") from exc


def _gemini_rest_fallback(
    api_key: str,
    model: str,
    messages: List[Dict[str, str]],
    system_prompt: str,
    max_output_tokens: int,
    temperature: float,
    timeout: float,
    effort: Optional[str] = None,
) -> str:
    """Direct REST request to Gemini v1beta API when SDK is not present."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    
    contents: List[Dict[str, Any]] = []
    for msg in messages:
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append({
            "role": role,
            "parts": [{"text": msg["content"]}],
        })

    payload: Dict[str, Any] = {"contents": contents}
    generation_config: Dict[str, Any] = {"temperature": temperature}
    if max_output_tokens > 0:
        generation_config["maxOutputTokens"] = max_output_tokens
    if effort is not None:
        generation_config["thinkingConfig"] = gemini_thinking_config(model, effort, rest=True)
    payload["generationConfig"] = generation_config

    if system_prompt:
        payload["systemInstruction"] = {
            "parts": [{"text": system_prompt}]
        }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_text = exc.read().decode("utf-8", errors="replace")
        raise GeminiAPIError(f"Gemini REST API error ({exc.code}): {err_text}", retryable=exc.code in {429, 500, 503}) from exc
    except Exception as exc:
        raise GeminiAPIError(f"Gemini REST request failed: {exc}", retryable=True) from exc

    usage = data.get("usageMetadata", {})
    record_token_usage(usage.get("promptTokenCount"), usage.get("candidatesTokenCount"))

    candidates = data.get("candidates", [])
    if not candidates:
        raise GeminiAPIError("Gemini returned no candidates.")

    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise GeminiAPIError("Gemini candidate had no text.")

    return text


def gemini_chat(
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
    """Send a multi-turn chat request to Gemini."""
    api_key = get_gemini_api_key()
    effort = resolve_effort("gemini", model, effort)

    # If SDK client is None or not usable, use REST fallback
    if client is None:
        if not api_key:
            raise GeminiAPIError("Set GEMINI_API_KEY to use Gemini.")
        return _gemini_rest_fallback(
            api_key=api_key,
            model=model,
            messages=messages,
            system_prompt=system_prompt,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            timeout=timeout,
            effort=effort,
        )

    # Use google-genai SDK
    try:
        sdk_contents = []
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            sdk_contents.append({"role": role, "parts": [{"text": msg["content"]}]})

        config: Dict[str, Any] = {"temperature": temperature}
        if effort is not None:
            config["thinking_config"] = gemini_thinking_config(model, effort)
        if max_output_tokens > 0:
            config["max_output_tokens"] = max_output_tokens
        if system_prompt:
            config["system_instruction"] = system_prompt

        response = getattr(client, "models").generate_content(
            model=model,
            contents=sdk_contents,
            config=config,
        )

        usage = getattr(response, "usage_metadata", None)
        if usage:
            record_token_usage(
                getattr(usage, "prompt_token_count", None),
                getattr(usage, "candidates_token_count", None),
            )

        text = getattr(response, "text", "")
        if not text:
            raise GeminiAPIError("Gemini returned empty text.")
        return text.strip()

    except Exception as exc:
        if isinstance(exc, GeminiAPIError):
            raise
        # Try REST fallback if SDK invocation failed
        if api_key:
            try:
                return _gemini_rest_fallback(
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    system_prompt=system_prompt,
                    max_output_tokens=max_output_tokens,
                    temperature=temperature,
                    timeout=timeout,
                    effort=effort,
                )
            except Exception:
                pass
        raise GeminiAPIError(f"Gemini API failed: {exc}", retryable=True) from exc
