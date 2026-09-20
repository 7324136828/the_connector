"""Internal request/response diagnostics without changing HTTP or SSE delivery."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import uuid
from urllib.parse import parse_qsl

import anyio
from fastapi import FastAPI

from .audit_context import start_audit_context, get_audit_context, finish_audit_context

logger = logging.getLogger(__name__)

_PRIVATE_QUERY_KEYS = {"authorization", "api_key", "apikey", "access_token", "refresh_token", "token", "password", "secret", "key"}


def _query_parameters(query: bytes) -> list[dict]:
    return [
        {"name": name, "value": "[REDACTED]" if name.lower() in _PRIVATE_QUERY_KEYS else value}
        for name, value in parse_qsl(query.decode("utf-8", errors="replace"), keep_blank_values=True)
    ]


def _decode_body(captured: bytearray, content_type: str, truncated: bool):
    if not captured:
        return None
    text = captured.decode("utf-8", errors="replace")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if not truncated and (media_type == "application/json" or media_type.endswith("+json")):
        try:
            return json.loads(text)
        except ValueError:
            pass
    return text

_ROUTES = [
    ("GET", "/api/config-history", "get_api_config-history.jsonl"),
    ("GET", "/api/configs", "get_api_configs.jsonl"),
    ("GET", "/api/models", "get_api_models.jsonl"),
    ("GET", "/api/providers/models", "get_api_providers_models.jsonl"),
    ("GET", "/api/sessions", "get_api_sessions.jsonl"),
    ("GET", "/api/sessions/{session_id}", "get_api_sessions_session_id.jsonl"),
    ("GET", "/api/v1/models", "get_api_v1_models.jsonl"),
    ("GET", "/api/v1/models/{model_id}", "get_api_v1_models_model_id.jsonl"),
    ("POST", "/api/api/show", "post_api_api_show.jsonl"),
    ("POST", "/api/chat", "post_api_chat.jsonl"),
    ("POST", "/api/chat/completions", "post_api_chat_completions.jsonl"),
    ("POST", "/api/config-history", "post_api_config-history.jsonl"),
    ("POST", "/api/config/validate", "post_api_config_validate.jsonl"),
    ("POST", "/api/configs", "post_api_configs.jsonl"),
    ("POST", "/api/models/capabilities", "post_api_models_capabilities.jsonl"),
    ("POST", "/api/sessions", "post_api_sessions.jsonl"),
]
_MATCHERS = [
    (method, template, filename, re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", template) + "$"))
    for method, template, filename in _ROUTES
]


def _route_match(method: str, path: str):
    path = path.rstrip("/")
    return next(((template, filename) for verb, template, filename, pattern in _MATCHERS
                 if verb == method and pattern.fullmatch(path)), None)


def route_log_file(method: str, path: str) -> str | None:
    match = _route_match(method, path)
    return match[1] if match else None


@contextmanager
def _file_lock(directory: Path):
    """Serialize rotation/appends across server workers on Windows and Unix."""
    with (directory / ".response-logs.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class ResponseLogWriter:
    """One UTF-8 JSONL file per endpoint family, with numbered rotated backups."""

    def __init__(self, directory: Path, enabled: bool = True,
                 max_bytes: int = 10 * 1024 * 1024, backup_count: int = 5):
        self.directory = Path(directory)
        self.enabled = enabled
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = threading.Lock()
        self._failure_reported = False

    def write(self, filename: str, record: dict) -> None:
        if not self.enabled:
            return
        # Filenames come only from the fixed route map, never from an ID or URL.
        if filename not in {item[2] for item in _ROUTES}:
            raise ValueError("Unknown response log filename.")
        line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            with _file_lock(self.directory):
                path = self.directory / filename
                if path.exists() and path.stat().st_size and path.stat().st_size + len(line) > self.max_bytes:
                    for number in range(self.backup_count, 0, -1):
                        previous = path if number == 1 else path.with_name(f"{filename}.{number - 1}")
                        if previous.exists():
                            previous.replace(path.with_name(f"{filename}.{number}"))
                with path.open("ab") as output:
                    output.write(line)

    def report_failure(self, error: Exception) -> None:
        # Avoid repeating errors for every request, or exposing the response body
        # through standard logging's exception/formatting diagnostics.
        with self._lock:
            if not self._failure_reported:
                self._failure_reported = True
                logger.warning("Response logging failed (%s) in %s; HTTP responses are unaffected.",
                               type(error).__name__, self.directory)


class ResponseLoggingMiddleware:
    def __init__(self, app, writer: ResponseLogWriter, max_body_bytes: int = 0,
                 audit_store=None, max_request_body_bytes: int = 0):
        self.app = app
        self.writer = writer
        self.max_body_bytes = max_body_bytes
        self.audit_store = audit_store
        self.max_request_body_bytes = max_request_body_bytes

    async def __call__(self, scope, receive, send):
        match = _route_match(scope.get("method", ""), scope.get("path", ""))
        database_enabled = self.audit_store is not None and self.audit_store.enabled
        if scope["type"] != "http" or not (self.writer.enabled or database_enabled) or match is None:
            return await self.app(scope, receive, send)

        route, filename = match
        started = time.perf_counter()
        captured = bytearray()
        request_captured = bytearray()
        request_bytes = 0
        request_headers = dict(scope.get("headers", []))
        request_content_type = request_headers.get(b"content-type", b"").decode("latin-1")
        request_complete = request_headers.get(b"content-length", b"0") == b"0" and b"transfer-encoding" not in request_headers
        status_code = None
        content_type = ""
        body_bytes = 0
        complete = False
        error_type = None
        context_token = start_audit_context()

        async def capture_receive():
            nonlocal request_bytes, request_complete
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                request_bytes += len(chunk)
                remaining = max(0, self.max_request_body_bytes - len(request_captured)) if self.max_request_body_bytes else len(chunk)
                request_captured.extend(chunk[:remaining])
                request_complete = not message.get("more_body", False)
            return message

        async def capture_send(message):
            nonlocal status_code, content_type, body_bytes, complete
            if message["type"] == "http.response.start":
                status_code = message["status"]
                content_type = next((value.decode("latin-1") for key, value in message.get("headers", [])
                                     if key.lower() == b"content-type"), "")
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                body_bytes += len(chunk)
                remaining = max(0, self.max_body_bytes - len(captured)) if self.max_body_bytes else len(chunk)
                captured.extend(chunk[:remaining])
            # Forward the original ASGI message immediately, including SSE chunks.
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True

        try:
            await self.app(scope, capture_receive, capture_send)
        except BaseException as exc:
            error_type = type(exc).__name__
            raise
        finally:
            routing = get_audit_context()
            finish_audit_context(context_token)
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "request_id": str(uuid.uuid4()),
                "method": scope["method"], "path": scope["path"], "route": route,
                "status_code": status_code, "content_type": content_type,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "body_bytes": body_bytes, "captured_bytes": len(captured),
                "truncated": len(captured) < body_bytes, "complete": complete,
                "request": {
                    "content_type": request_content_type,
                    "query": _query_parameters(scope.get("query_string", b"")),
                    "body_bytes": request_bytes, "captured_bytes": len(request_captured),
                    "truncated": len(request_captured) < request_bytes, "complete": request_complete,
                },
                "routing": routing,
            }
            if error_type:
                record["error_type"] = error_type

            def persist():
                # Decoding/serialization and disk I/O stay off the event loop.
                try:
                    record["body"] = _decode_body(captured, content_type, record["truncated"])
                    request_body = _decode_body(request_captured, request_content_type, record["request"]["truncated"])
                    record["request"]["body"] = request_body
                    if isinstance(request_body, dict):
                        for key, request_key in (("requested_model", "model"), ("session_id", "session_id")):
                            value = request_body.get(request_key)
                            if routing.get(key) is None and isinstance(value, str):
                                routing[key] = value[:500]
                except Exception as exc:
                    self.writer.report_failure(exc)
                    return
                # Each sink is independent: a full disk or locked database must
                # not suppress the other record or change the client's response.
                if self.writer.enabled:
                    try:
                        self.writer.write(filename, record)
                    except Exception as exc:
                        self.writer.report_failure(exc)
                if database_enabled:
                    try:
                        self.audit_store.write(record)
                    except Exception as exc:
                        self.audit_store.report_failure(exc)

            # Preserve even a disconnected client's partial stream, without
            # swallowing the cancellation/error that ended the original request.
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(persist)


class LoggedFastAPI(FastAPI):
    def __init__(self, *, response_log_writer: ResponseLogWriter,
                 response_log_max_body_bytes: int = 0, internal_audit_store=None,
                 request_log_max_body_bytes: int = 0, **kwargs):
        super().__init__(**kwargs)
        self.response_log_writer = response_log_writer
        self.response_log_max_body_bytes = response_log_max_body_bytes
        self.internal_audit_store = internal_audit_store
        self.request_log_max_body_bytes = request_log_max_body_bytes

    def build_middleware_stack(self):
        # Wrapping outside ServerErrorMiddleware captures its actual 500 response.
        return ResponseLoggingMiddleware(
            super().build_middleware_stack(), self.response_log_writer,
            self.response_log_max_body_bytes,
            audit_store=self.internal_audit_store,
            max_request_body_bytes=self.request_log_max_body_bytes,
        )
