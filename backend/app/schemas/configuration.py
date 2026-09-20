"""Validation and normalization of the config.json snapshot owned by a session."""

from copy import deepcopy
import math
from typing import Any

from ..model_capabilities import resolve_effort

PROVIDERS = {"openai", "claude", "gemini", "openrouter", "ollama", "mock"}
DEFAULT_SYSTEM_PROMPT = "You are a helpful, precise, and thoughtful AI assistant."


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}.")
    return value


def _keys(value: dict, allowed: set, path: str) -> None:
    unknown = value.keys() - allowed
    if unknown:
        raise ValueError(f"Unknown {path} field(s): {', '.join(sorted(unknown))}.")


def _route(value: Any, path: str, default_retries: int = 1, choice: bool = False) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object.")
    route = deepcopy(value)
    _keys(route, {"provider", "model", "effort", "retries", "probability"} if choice else {"provider", "model", "effort", "retries", "type"}, path)
    provider = route.get("provider")
    if not isinstance(provider, str):
        raise ValueError(f"{path}.provider is required.")
    provider = {"anthropic": "claude", "google": "gemini", "demo": "mock"}.get(provider.lower().strip(), provider.lower().strip())
    if provider not in PROVIDERS:
        raise ValueError(f"Unsupported provider at {path}: {provider}.")
    model = route.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"{path}.model must be a nonempty string.")
    route.update(provider=provider, model=model.strip())
    route["retries"] = _integer(route.get("retries", default_retries), f"{path}.retries", 0, 5)
    effort = resolve_effort(provider, model.strip(), route.get("effort"))
    if effort is None:
        route.pop("effort", None)
    else:
        route["effort"] = effort
    if choice:
        weight = route.get("probability", 1)
        if type(weight) not in (int, float) or not math.isfinite(weight) or weight < 0 or weight > 1_000_000:
            raise ValueError(f"{path}.probability must be a finite nonnegative number up to 1000000.")
        route["probability"] = weight
    route.pop("type", None)
    return route


def normalize_config(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("config must be a config.json object.")
    config = deepcopy(value)
    _keys(config, {"sequences", "system_prompt", "past_memory", "context_window", "memory_window", "memory_scope"}, "config")
    steps = config.get("sequences")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 30:
        raise ValueError("config.sequences must contain between 1 and 30 routing steps.")
    normalized = []
    for index, step in enumerate(steps):
        path = f"sequences[{index}]"
        if isinstance(step, dict) and step.get("type") == "probability":
            _keys(step, {"type", "choices", "retries"}, path)
            choices = step.get("choices")
            if not isinstance(choices, list) or not 1 <= len(choices) <= 30:
                raise ValueError(f"{path}.choices must contain between 1 and 30 routes.")
            retries = _integer(step.get("retries", 1), f"{path}.retries", 0, 5)
            choices = [_route(c, f"{path}.choices[{i}]", retries, True) for i, c in enumerate(choices)]
            if not any(c["probability"] > 0 for c in choices):
                raise ValueError(f"{path} must contain at least one positive probability.")
            normalized.append({"type": "probability", "choices": choices, "retries": retries})
        else:
            if isinstance(step, dict) and step.get("type") not in (None, "direct"):
                raise ValueError(f"{path}.type must be 'direct' or 'probability'.")
            normalized.append(_route(step, path))
    config["sequences"] = normalized
    prompt = config.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
    if not isinstance(prompt, str) or len(prompt) > 100_000:
        raise ValueError("system_prompt must be a string of at most 100000 characters.")
    config["system_prompt"] = prompt
    memory = config.get("past_memory", True)
    if type(memory) is not bool:
        raise ValueError("past_memory must be true or false.")
    config["past_memory"] = memory
    config["context_window"] = _integer(config.get("context_window", 10), "context_window", 1, 200)
    config["memory_window"] = _integer(config.get("memory_window", 20), "memory_window", 0, 200)
    scope = config.get("memory_scope", "all_sessions")
    if scope not in ("all_sessions", "session"):
        raise ValueError("memory_scope must be 'all_sessions' or 'session'.")
    config["memory_scope"] = scope
    return config


def example_config() -> dict:
    """An example only; sessions never load the server's root config.json."""
    return normalize_config({
        "sequences": [{"provider": "openai", "model": "gpt-5-nano", "effort": "minimal", "retries": 1}],
    })
