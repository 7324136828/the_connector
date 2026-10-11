"""Shared effort settings for the UI and provider adapters.

OpenAI routes accept effort independently of model names; the upstream API
validates model-specific support. Other direct providers use a conservative
registry that must stay in sync with their documentation.
"""

import re
from typing import Optional

OPENAI_EFFORT_LEVELS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
OPENROUTER_EFFORT_LEVELS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]


def effort_levels(provider: str, model: str) -> list[str]:
    provider = {"anthropic": "claude", "google": "gemini", "demo": "mock"}.get(provider, provider)
    model = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model)
    if provider == "openrouter":
        # OpenRouter exposes one normalized reasoning-effort vocabulary and
        # translates it for the selected upstream model.
        return OPENROUTER_EFFORT_LEVELS.copy()
    if provider == "openai":
        return OPENAI_EFFORT_LEVELS.copy()
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
    if provider == "openai":
        return "minimal"
    if provider == "openrouter":
        # Let OpenRouter/the upstream provider choose unless the route opts in.
        return None
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
