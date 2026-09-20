"""Shared, conservative effort capabilities for the UI and provider adapters.

Unknown models remain usable without an effort override. Keep this registry in
sync with the provider documentation before advertising additional levels.
"""

import re
from typing import Optional


def effort_levels(provider: str, model: str) -> list[str]:
    provider = {"anthropic": "claude", "google": "gemini", "demo": "mock"}.get(provider, provider)
    model = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model)
    if provider == "openrouter":
        owner, _, name = model.partition("/")
        if owner == "anthropic":
            # OpenRouter spells version segments with dots (claude-opus-4.6).
            name = name.replace(".", "-")
        return effort_levels({"anthropic": "claude", "google": "gemini"}.get(owner, owner), name)
    if provider == "openai":
        if model in {"gpt-5", "gpt-5-mini", "gpt-5-nano"}:
            return ["minimal", "low", "medium", "high"]
        if model == "gpt-5.1":
            return ["none", "low", "medium", "high"]
        if model == "gpt-5.2":
            return ["none", "low", "medium", "high", "xhigh"]
        if model == "gpt-5.6-luna":
            return ["none", "low", "medium", "high", "xhigh", "max"]
        if model in {"o1", "o3", "o3-mini", "o4-mini"}:
            return ["low", "medium", "high"]
    if provider == "claude":
        model = re.sub(r"-\d{8}$", "", model)
        if model in {"claude-opus-4-5", "claude-sonnet-4-6"}:
            return ["low", "medium", "high"]
        if model == "claude-opus-4-6":
            return ["low", "medium", "high", "max"]
    if provider == "gemini":
        if model in {"gemini-2.5-flash", "gemini-2.5-flash-lite"}:
            return ["none", "low", "medium", "high"]
        if model == "gemini-2.5-pro":
            return ["low", "medium", "high"]
    if provider == "ollama" and model.split(":")[0] == "gpt-oss":
        return ["low", "medium", "high"]
    return []


def default_effort(provider: str, model: str) -> Optional[str]:
    levels = effort_levels(provider, model)
    return levels[0] if levels else None


def resolve_effort(provider: str, model: str, effort: Optional[str]) -> Optional[str]:
    levels = effort_levels(provider, model)
    if effort is None:
        return default_effort(provider, model)
    if effort not in levels:
        supported = ", ".join(levels) or "none (this model has no supported effort control)"
        raise ValueError(f"Unsupported effort '{effort}' for {provider}/{model}. Supported: {supported}.")
    return effort


def gemini_thinking_config(model: str, effort: Optional[str], *, rest: bool = False) -> dict:
    """Named UI levels map to explicit Gemini 2.5 token budgets."""
    if effort is None:
        return {}
    budget = {"none": 0, "low": 1024, "medium": 8192, "high": 24576}[effort]
    return {"thinkingBudget" if rest else "thinking_budget": budget}
