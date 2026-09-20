"""Bounded routing facts shared by an ASGI request and its worker threads.

The ContextVar holds a mutable dictionary: FastAPI's copied thread context sees
the same request-owned object, so routing updates are available to middleware.
This module deliberately never retains prompts, completions, or error messages.
"""

from contextvars import ContextVar, Token
import math


MAX_AUDIT_ATTEMPTS = 6000
_audit_context: ContextVar[dict | None] = ContextVar("request_routing_audit", default=None)


def start_audit_context() -> Token:
    return _audit_context.set({
        "performed": False,
        "requested_model": None,
        "configuration_id": None,
        "session_id": None,
        "selected": None,
        "attempts": [],
    })


def get_audit_context() -> dict | None:
    return _audit_context.get()


def finish_audit_context(token: Token) -> None:
    _audit_context.reset(token)


def _label(value, limit: int = 1024) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def set_audit_context(**metadata) -> None:
    """Attach recognized routing identifiers; ignore arbitrary payload fields."""
    context = _audit_context.get()
    if context is None:
        return
    for key in ("requested_model", "configuration_id", "session_id"):
        if key in metadata:
            context[key] = _label(metadata[key])


def record_route_attempt(
    provider: str, model: str, effort: str | None, status: str, *,
    attempt: int = 1, elapsed_ms: float = 0, returned_model: str | None = None,
    error_type: str | None = None, routing_type: str | None = None,
) -> None:
    """Record an attempted route and the actual successful selection, if any."""
    context = _audit_context.get()
    if context is None:
        return
    context["performed"] = True
    elapsed = elapsed_ms if isinstance(elapsed_ms, (int, float)) and math.isfinite(elapsed_ms) else 0
    entry = {
        "provider": _label(provider, 100), "model": _label(model),
        "effort": _label(effort, 100), "status": _label(status, 100),
        "attempt": attempt if type(attempt) is int and attempt > 0 else 1,
        "elapsed_ms": round(max(0, elapsed), 3),
    }
    actual_model = _label(returned_model)
    if actual_model:
        entry["returned_model"] = actual_model
    if error_type:
        entry["error_type"] = _label(error_type, 200)
    if routing_type:
        entry["routing_type"] = _label(routing_type, 100)
    if len(context["attempts"]) < MAX_AUDIT_ATTEMPTS:
        context["attempts"].append(entry)
    else:
        context["attempts_dropped"] = context.get("attempts_dropped", 0) + 1
    if status == "success":
        context["selected"] = {
            "provider": entry["provider"], "model": actual_model or entry["model"],
            "configured_model": entry["model"], "effort": entry["effort"],
        }
