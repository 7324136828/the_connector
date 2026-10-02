import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
from zipfile import ZipFile

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(ROOT))
from count_message import count_messages, default_database_path
from count_message.server import create_server, register_tool
from build_plugin import build


class MessageCountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "counts #1.db"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE sessions(id TEXT PRIMARY KEY, user_session INTEGER, status TEXT);
                CREATE TABLE messages(session_id TEXT, role TEXT, content TEXT);
                INSERT INTO sessions VALUES ('web', 1, 'active'), ('closed', 1, 'closed'),
                    ('system', 0, 'active'), ('empty', 1, 'active');
                INSERT INTO messages VALUES ('web', 'user', 'hello'), ('web', 'assistant', 'hi'),
                    ('closed', 'user', 'earlier'), ('closed', 'assistant', 'reply'),
                    ('system', 'user', 'system input'), ('system', 'assistant', 'system output'),
                    ('web', 'system', 'prompt'), ('web', 'tool', 'observation'),
                    ('web', 'assistant', ''), ('web', 'user', '  ');
            """)
            conn.execute("INSERT INTO messages VALUES ('web', 'user', ?)", ("\t\n\u2003",))
        self.server = create_server(port=0, db_path=self.db, ui_dir=ROOT / "ui")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path="/api/count_message", body=None, headers=None):
        args = {"headers": headers or {}}
        if body is not None:
            args["data"] = json.dumps(body).encode()
            args["headers"] = {"Content-Type": "application/json", **args["headers"]}
        return urlopen(Request(self.base + path, **args), timeout=5)

    def test_global_counts_closed_and_empty_sessions_without_system(self):
        result = count_messages(self.db)
        self.assertEqual((result["user_messages"], result["assistant_messages"], result["total_messages"], result["sessions"]), (2, 2, 4, 3))
        self.assertEqual(count_messages(self.db, include_system_sessions=True)["total_messages"], 6)

    def test_session_filters_and_injection(self):
        self.assertEqual(count_messages(self.db, session_id="web")["total_messages"], 2)
        self.assertEqual(count_messages(self.db, session_id="empty")["total_messages"], 0)
        self.assertEqual(count_messages(self.db, session_id="system")["total_messages"], 0)
        self.assertEqual(count_messages(self.db, session_id="system", include_system_sessions=True)["total_messages"], 2)
        with self.assertRaises(LookupError):
            count_messages(self.db, session_id="' OR 1=1 --")

    def test_no_sidebar_limit_and_live_updates(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executemany("INSERT INTO sessions VALUES (?, 1, 'active')", [(f"s{i}",) for i in range(60)])
            conn.executemany("INSERT INTO messages VALUES (?, 'user', 'text')", [(f"s{i}",) for i in range(60)])
        self.assertEqual(count_messages(self.db)["total_messages"], 64)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM messages")
        self.assertEqual(count_messages(self.db)["total_messages"], 0)

    def test_missing_database_is_not_created_and_count_is_readonly(self):
        before = self.db.read_bytes()
        count_messages(self.db)
        self.assertEqual(self.db.read_bytes(), before)
        missing = self.db.parent / "missing.db"
        with self.assertRaises(sqlite3.Error):
            count_messages(missing)
        self.assertFalse(missing.exists())

    def test_environment_resolution(self):
        with patch.dict(os.environ, {"DB_PATH": str(self.db), "DATA_DIR": "ignored"}):
            self.assertEqual(default_database_path(), self.db)
        with patch.dict(os.environ, {"DB_PATH": "", "DATA_DIR": str(self.db.parent)}):
            self.assertEqual(default_database_path(), self.db.parent / "connector.db")

    def test_get_post_cors_and_health(self):
        with self.request(headers={"Origin": "http://localhost:5130"}) as response:
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "http://localhost:5130")
            self.assertEqual(json.load(response)["total_messages"], 4)
        with self.request(body={"session_id": "closed", "include_system_sessions": True}) as response:
            self.assertEqual(json.load(response)["total_messages"], 2)
        with self.request("/health") as response:
            self.assertEqual(json.load(response)["status"], "ok")
        with self.request(headers={"Origin": "http://untrusted.example"}) as response:
            self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))

    def test_http_validation_and_unavailable_database(self):
        for path, body, status in [
            ("/api/count_message?session_id=missing", None, 404),
            ("/api/count_message?session_id=", None, 400),
            ("/api/count_message?include_system_sessions=0", None, 400),
            ("/api/count_message?session_id=web&session_id=closed", None, 400),
            ("/api/count_message", {"include_system_sessions": "false"}, 400),
            ("/api/count_message", {"db_path": "secret"}, 400),
            ("/api/count_message", [], 400),
            ("/backend/count_message/server.py", None, 404),
        ]:
            with self.subTest(path=path, body=body):
                with self.assertRaises(HTTPError) as error:
                    self.request(path, body)
                self.assertEqual(error.exception.code, status)
                error.exception.close()
        self.db.unlink()
        with self.assertRaises(HTTPError) as error:
            self.request()
        self.assertEqual(error.exception.code, 503)
        error.exception.close()

    def test_standalone_ui_assets(self):
        with self.request("/") as response:
            self.assertIn(b"mount(document.getElementById", response.read())
        with self.request("/src/index.js") as response:
            self.assertIn(b"export function mount", response.read())

    def test_registration_matches_connector_webhook_contract(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        captured = {}
        class ConnectorHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                captured["path"] = self.path
                self.send_response(201)
                self.end_headers()
                self.wfile.write(b'{"status":"registered"}')
        connector = ThreadingHTTPServer(("127.0.0.1", 0), ConnectorHandler)
        worker = threading.Thread(target=connector.serve_forever, daemon=True)
        worker.start()
        try:
            endpoint = self.base + "/api/count_message"
            register_tool(f"http://127.0.0.1:{connector.server_port}", endpoint)
            self.assertEqual(captured["name"], "count_message")
            self.assertEqual(captured["endpoint"], endpoint)
            self.assertEqual(captured["path"], "/api/agent/register-tool")
            with self.request(body={}) as response:
                self.assertEqual(json.load(response)["total_messages"], 4)
        finally:
            connector.shutdown()
            connector.server_close()
            worker.join()

    def test_zip_layout_and_reproducible_build(self):
        target = Path(self.temp.name) / "plugin.zip"
        build(target)
        first = target.read_bytes()
        build(target)
        self.assertEqual(target.read_bytes(), first)
        with ZipFile(target) as archive:
            names = archive.namelist()
            for name in ("instruction.md", "plugin.json", "run.bat", "run.sh", "ui/package.json", "backend/pyproject.toml"):
                self.assertIn(name, names)
            self.assertFalse(any("__pycache__" in name or name.endswith(".db") for name in names))
            self.assertTrue(archive.getinfo("run.sh").external_attr >> 16 & 0o111)

    def test_extracted_zip_launcher(self):
        target = Path(self.temp.name) / "plugin.zip"
        extracted = Path(self.temp.name) / "extracted plugin"
        build(target)
        with ZipFile(target) as archive:
            archive.extractall(extracted)
        process = subprocess.Popen(
            [sys.executable, str(extracted / "run.py"), "--port", "0", "--db-path", str(self.db)],
            cwd=self.temp.name, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            startup = process.stdout.readline().strip()
            self.assertTrue(startup.startswith("count_message UI/API: http://127.0.0.1:"), startup)
            base = startup.split("UI/API: ", 1)[1]
            with urlopen(base + "/api/count_message", timeout=5) as response:
                self.assertEqual(json.load(response)["total_messages"], 4)
            with urlopen(base + "/", timeout=5) as response:
                self.assertIn(b"count_message", response.read())
        finally:
            process.terminate()
            process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
