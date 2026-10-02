"""Dependency-free HTTP backend and optional UI server."""

from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.request
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .counter import count_messages, default_database_path

MAX_BODY = 8192
DEFAULT_ORIGINS = (
    "http://localhost:5130", "http://127.0.0.1:5130",
    "http://localhost:5133", "http://127.0.0.1:5133",
)


def validate_arguments(value):
    if not isinstance(value, dict) or set(value) - {"session_id", "include_system_sessions"}:
        raise ValueError("Expected an object with session_id and/or include_system_sessions")
    session_id = value.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
        raise ValueError("session_id must be a nonblank string or null")
    include = value.get("include_system_sessions", False)
    if not isinstance(include, bool):
        raise ValueError("include_system_sessions must be a boolean")
    return {"session_id": session_id, "include_system_sessions": include}


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, db_path, ui_dir=None, allowed_origins=DEFAULT_ORIGINS, **kwargs):
        self.db_path = db_path
        self.ui_dir = ui_dir
        self.allowed_origins = allowed_origins
        super().__init__(*args, **kwargs)

    def reply(self, code, value, content_type="application/json; charset=utf-8"):
        data = json.dumps(value).encode("utf-8") if content_type.startswith("application/json") else value
        self.send_response(code)
        origin = self.headers.get("Origin")
        if origin in self.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.reply(200, {})

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/api/count_message":
            try:
                query = parse_qs(url.query, keep_blank_values=True)
                if set(query) - {"session_id", "include_system_sessions"} or any(len(v) != 1 for v in query.values()):
                    raise ValueError("Unknown or duplicate query parameters")
                args = {key: values[0] for key, values in query.items()}
                if "include_system_sessions" in args:
                    if args["include_system_sessions"] not in ("true", "false"):
                        raise ValueError("include_system_sessions must be true or false")
                    args["include_system_sessions"] = args["include_system_sessions"] == "true"
                self.count(args)
            except ValueError as exc:
                self.reply(400, {"detail": str(exc)})
            return
        if url.path == "/health":
            self.count({}, health=True)
            return
        # Only public UI assets are exposed, never backend files or database paths.
        assets = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
                  "/src/index.js": ("src/index.js", "text/javascript")}
        if self.ui_dir and url.path in assets:
            name, mime = assets[url.path]
            try:
                self.reply(200, (self.ui_dir / name).read_bytes(), mime + "; charset=utf-8")
            except OSError:
                self.reply(404, {"detail": "UI asset not found"})
            return
        self.reply(404, {"detail": "Not found"})

    def do_POST(self):
        if urlsplit(self.path).path != "/api/count_message":
            self.reply(404, {"detail": "Not found"})
            return
        try:
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                self.reply(415, {"detail": "Use application/json"})
                return
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                self.reply(413, {"detail": "Expected a JSON body of at most 8192 bytes"})
                return
            args = json.loads(self.rfile.read(size))
            self.count(args)
        except (ValueError, UnicodeError) as exc:
            self.reply(400, {"detail": str(exc)})

    def count(self, args, health=False):
        try:
            result = count_messages(self.db_path, **validate_arguments(args))
            self.reply(200, {"status": "ok", "plugin": "count_message"} if health else result)
        except ValueError as exc:
            self.reply(400, {"detail": str(exc)})
        except LookupError as exc:
            self.reply(404, {"detail": str(exc)})
        except sqlite3.Error:
            self.reply(503, {"detail": "Connector database unavailable. Start Connector and check --db-path."})


def create_server(host="127.0.0.1", port=8403, *, db_path=None, ui_dir=None, allowed_origins=DEFAULT_ORIGINS):
    return ThreadingHTTPServer((host, port), partial(
        Handler, db_path=db_path or default_database_path(),
        ui_dir=Path(ui_dir) if ui_dir else None, allowed_origins=allowed_origins,
    ))


def register_tool(connector_url, endpoint):
    definition = {
        "name": "count_message",
        "description": "Count retained user and assistant text messages across chats or in session_id. Includes closed chats; system sessions are excluded by default.",
        "parameters": {"type": "object", "properties": {
            "session_id": {"type": "string", "description": "Omit for all saved chats"},
            "include_system_sessions": {"type": "boolean", "default": False},
        }, "additionalProperties": False},
        "endpoint": endpoint,
    }
    request = urllib.request.Request(connector_url.rstrip("/") + "/api/agent/register-tool",
                                     data=json.dumps(definition).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8403)
    parser.add_argument("--db-path", type=Path, default=default_database_path())
    parser.add_argument("--ui-dir", type=Path)
    parser.add_argument("--allow-origin", action="append", default=[])
    parser.add_argument("--register", metavar="CONNECTOR_URL", help="Register webhook with a running Connector")
    parser.add_argument("--endpoint", help="Webhook URL reachable from Connector (for remote installations)")
    args = parser.parse_args()
    server = create_server(args.host, args.port, db_path=args.db_path, ui_dir=args.ui_dir,
                           allowed_origins=(*DEFAULT_ORIGINS, *args.allow_origin))
    try:
        if args.register:
            endpoint = args.endpoint or f"http://127.0.0.1:{server.server_port}/api/count_message"
            register_tool(args.register, endpoint)
            print("Registered count_message; register again after restarting Connector.", flush=True)
        print(f"count_message UI/API: http://{args.host}:{server.server_port}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
