"""Intelligent routing engine supporting weighted probability and fallback sequences."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..config import settings
from ..audit_context import record_route_attempt
from ..schemas.configuration import normalize_config
from .connectors import (
    ChatAPIError,
    capture_token_usage,
    create_claude_client,
    create_gemini_client,
    create_openai_client,
    create_openrouter_client,
    claude_chat,
    gemini_chat,
    mock_chat,
    ollama_chat,
    openai_chat,
    openrouter_chat,
    estimate_tokens,
)

@dataclass
class RouteResult:
    """Result of routing a chat request through providers."""
    content: str
    provider: str
    model: str
    tokens: Dict[str, Optional[int]]
    latency_ms: float
    attempt_info: Dict[str, Any]


class Router:
    """Dispatches chat completions across multiple LLM providers."""

    def __init__(self):
        # Cache client objects to avoid repeated initialization
        self._openai_client = None
        self._claude_client = None
        self._gemini_client = None
        self._openrouter_client = None

    def _execute_single_provider(
        self,
        provider: str,
        model: str,
        messages: List[Dict[str, str]],
        system_prompt: str = "",
        max_output_tokens: int = -1,
        temperature: float = 0.7,
        timeout: float = 60.0,
        effort: Optional[str] = None,
    ) -> str:
        """Call a specific provider connector directly."""
        p = provider.lower().strip()
        m = model.strip()

        if p == "mock" or p == "demo":
            return mock_chat(model=m, messages=messages, system_prompt=system_prompt)

        elif p == "openai":
            if self._openai_client is None:
                self._openai_client = create_openai_client(timeout=timeout)
            return openai_chat(
                self._openai_client,
                model=m,
                messages=messages,
                system_prompt=system_prompt,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                effort=effort,
            )

        elif p == "claude" or p == "anthropic":
            if self._claude_client is None:
                self._claude_client = create_claude_client(timeout=timeout)
            return claude_chat(
                self._claude_client,
                model=m,
                messages=messages,
                system_prompt=system_prompt,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                effort=effort,
            )

        elif p == "gemini" or p == "google":
            if self._gemini_client is None:
                try:
                    self._gemini_client = create_gemini_client(timeout=timeout)
                except Exception:
                    self._gemini_client = None
            return gemini_chat(
                self._gemini_client,
                model=m,
                messages=messages,
                system_prompt=system_prompt,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                effort=effort,
                timeout=timeout,
            )

        elif p == "ollama":
            return ollama_chat(
                settings.ollama_host,
                model=m,
                messages=messages,
                system_prompt=system_prompt,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                effort=effort,
                timeout=timeout,
            )

        elif p == "openrouter":
            if self._openrouter_client is None:
                try:
                    self._openrouter_client = create_openrouter_client(timeout=timeout)
                except Exception:
                    self._openrouter_client = None
            return openrouter_chat(
                self._openrouter_client,
                model=m,
                messages=messages,
                system_prompt=system_prompt,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                effort=effort,
                timeout=timeout,
            )

        else:
            raise ChatAPIError(f"Unsupported provider: '{provider}'")

    def route_chat(
        self,
        messages: List[Dict[str, str]],
        system_prompt: str = "",
        config: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> RouteResult:
        """Execute only the routes in an explicit, validated config snapshot."""
        try:
            config = normalize_config(config)
        except ValueError as exc:
            raise ChatAPIError(str(exc)) from exc
        started = time.perf_counter()
        attempts = []
        last_error = None
        for step in config["sequences"]:
            routing_type = "sequence_step"
            if step.get("type") == "probability":
                routing_type = "probability"
                choices = step["choices"]
                chosen = random.choices(range(len(choices)), weights=[c["probability"] for c in choices], k=1)[0]
                candidates = [choices[chosen]] + [c for i, c in enumerate(choices) if i != chosen]
            else:
                candidates = [step]
            for route in candidates:
                for attempt in range(route["retries"] + 1):
                    attempt_started = time.perf_counter()
                    info = {"provider": route["provider"], "model": route["model"],
                            "effort": route.get("effort"), "attempt": attempt + 1}
                    with capture_token_usage() as usage:
                        try:
                            content = self._execute_single_provider(
                                provider=route["provider"], model=route["model"],
                                effort=route.get("effort"), messages=messages,
                                system_prompt=system_prompt, timeout=timeout,
                            )
                            attempts.append({**info, "status": "success"})
                            input_tokens = usage.input_tokens if usage.input_tokens is not None else estimate_tokens(system_prompt + "\n".join(m["content"] for m in messages))
                            output_tokens = usage.output_tokens if usage.output_tokens is not None else estimate_tokens(content)
                            record_route_attempt(
                                route["provider"], route["model"], route.get("effort"), "success",
                                attempt=attempt + 1, elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                                routing_type=routing_type,
                            )
                            return RouteResult(
                                content=content, provider=route["provider"], model=route["model"],
                                tokens={"input_tokens": input_tokens, "output_tokens": output_tokens,
                                        "total_tokens": input_tokens + output_tokens},
                                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                                attempt_info={"total_attempts": len(attempts), "attempts": attempts,
                                              "selected_choice": f"{route['provider']}/{route['model']}",
                                              "effort": route.get("effort"), "routing_type": routing_type},
                            )
                        except Exception as exc:
                            last_error = exc
                            record_route_attempt(
                                route["provider"], route["model"], route.get("effort"), "failed",
                                attempt=attempt + 1, elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                                error_type=type(exc).__name__, routing_type=routing_type,
                            )
                            attempts.append({**info, "status": "failed", "error": str(exc)})
                            if attempt < route["retries"]:
                                time.sleep(0.5)
        # Never silently introduce a provider (including mock) absent from config.
        raise ChatAPIError(f"All configured routes failed. Last error: {last_error}")


router = Router()
