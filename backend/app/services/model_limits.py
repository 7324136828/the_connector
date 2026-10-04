"""Per-route input budgets and output caps shared by both chat transports."""

import json

from .connectors.common import ChatAPIError, estimate_tokens


class InputContextLimitError(ChatAPIError):
    """The supplied conversation is too large for the configured input budget."""


def effective_token_limit(route: dict, direction: str, config: dict | None = None) -> int | None:
    """Use only supplied caps; an unspecified cap never participates in comparison."""
    caps = [route.get(f"max_{direction}_tokens"), route.get(f"max_{direction}_token"),
            (config or {}).get(f"gross_max_{direction}_token")]
    provided = [cap for cap in caps if cap is not None]
    return min(provided) if provided else None


def ensure_input_limit(route: dict, messages: list[dict], system_prompt: str = "",
                       options: dict | None = None, config: dict | None = None) -> None:
    limit = effective_token_limit(route, "input", config)
    if limit is None:
        return
    # Include role/structure overhead, system/memory instructions, tool history,
    # and schemas. This is a local estimate, not a provider-specific tokenizer.
    payload = {"messages": messages}
    if system_prompt:
        payload["system_prompt"] = system_prompt
    for key in ("tools", "tool_choice", "response_format"):
        if options and key in options:
            payload[key] = options[key]
    estimated = estimate_tokens(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    if estimated > limit:
        raise InputContextLimitError(
            f"Estimated input is {estimated} tokens, exceeding the input limit of {limit} "
            f"for {route['provider']}/{route['model']}. Increase the input limit or send less context."
        )


def completion_options_with_limit(route: dict, options: dict, config: dict | None = None) -> dict:
    """A model cap never increases a caller's requested output limit."""
    bounded = dict(options)
    limit = effective_token_limit(route, "output", config)
    if limit is not None:
        key = "max_tokens" if "max_tokens" in bounded else "max_completion_tokens"
        bounded[key] = min(bounded[key], limit) if key in bounded else limit
    return bounded
