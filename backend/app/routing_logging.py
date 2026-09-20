"""Console routing decisions with request-local client addresses."""

from contextvars import ContextVar
import logging


# Inherit Uvicorn's console handler, formatter, and configured log level.
logger = logging.getLogger("uvicorn.error.routing")
_client_address: ContextVar[str] = ContextVar("routing_client_address", default="unknown")


class RoutingLogContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        client = scope.get("client")
        address = f"{client[0]}:{client[1]}" if client else "unknown"
        # ContextVars follow FastAPI's sync workers and stay isolated per request.
        token = _client_address.set(address)
        try:
            await self.app(scope, receive, send)
        finally:
            _client_address.reset(token)


def log_probability_choice(provider: str, model: str) -> None:
    # Configured model IDs may contain control characters; keep one console line.
    model = model.encode("unicode_escape").decode("ascii")
    logger.info(
        "%s --- probabilistic chooser chose model %s (provider=%s)",
        _client_address.get(), model, provider,
    )
