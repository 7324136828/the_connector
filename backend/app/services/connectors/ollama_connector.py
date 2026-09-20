"""Ollama local LLM connector for multi-turn chat and model discovery."""

from __future__ import annotations

import json
import os
import socket
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Any, Dict, List, Optional
from ...model_capabilities import resolve_effort

from .common import (
    ChatAPIError,
    record_token_usage,
)

DEFAULT_OLLAMA_MODEL = "llama3.2"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"


class OllamaError(ChatAPIError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message, retryable=retryable, provider="ollama")


def ollama_base_url(host: Optional[str] = None) -> str:
    h = (host or os.environ.get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST).rstrip("/")
    return h[:-4] if h.endswith("/api") else h


def list_ollama_models(host: Optional[str] = None, timeout: float = 3.0) -> List[str]:
    """Discover loaded/installed Ollama models via /api/tags."""
    base_url = ollama_base_url(host)
    req = Request(f"{base_url}/api/tags", method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = data.get("models", [])
        return [m.get("name") for m in models if isinstance(m, dict) and "name" in m]
    except Exception:
        return []


def ollama_chat(
    host: Optional[str] = None,
    *,
    model: str,
    messages: List[Dict[str, str]],
    system_prompt: str = "",
    max_output_tokens: int = -1,
    context_window: Optional[int] = None,
    temperature: float = 0.7,
    timeout: float = 60.0,
    effort: Optional[str] = None,
) -> str:
    """Send a multi-turn chat request to Ollama's /api/chat endpoint."""
    base_url = ollama_base_url(host)
    url = f"{base_url}/api/chat"

    formatted_messages: List[Dict[str, str]] = []
    if system_prompt:
        formatted_messages.append({"role": "system", "content": system_prompt})
    for m in messages:
        formatted_messages.append({"role": m["role"], "content": m["content"]})

    options: Dict[str, Any] = {"temperature": temperature}
    if max_output_tokens > 0:
        options["num_predict"] = max_output_tokens
    if context_window is not None:
        options["num_ctx"] = context_window

    payload = {
        "model": model,
        "messages": formatted_messages,
        "stream": False,
        "options": options,
    }
    effort = resolve_effort("ollama", model, effort)
    if effort is not None:
        payload["think"] = effort
    body = json.dumps(payload).encode("utf-8")

    req = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")
        hint = f" Run 'ollama pull {model}' first." if exc.code == 404 else ""
        raise OllamaError(
            f"Ollama returned HTTP {exc.code}: {err}.{hint}",
            retryable=exc.code in {429, 500, 503},
        ) from exc
    except URLError as exc:
        raise OllamaError(
            f"Could not connect to Ollama at {base_url}. Is Ollama running? ({exc.reason})",
            retryable=True,
        ) from exc
    except Exception as exc:
        raise OllamaError(f"Ollama request failed: {exc}", retryable=True) from exc

    prompt_eval = data.get("prompt_eval_count")
    eval_count = data.get("eval_count")
    record_token_usage(prompt_eval, eval_count)

    msg = data.get("message", {})
    content = msg.get("content", "").strip()
    if not content:
        raise OllamaError("Ollama returned an empty message.")

    return content
