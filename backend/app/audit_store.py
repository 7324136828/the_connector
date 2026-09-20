"""Internal HTTP audit persistence, separate from user and LLM-visible data."""

from contextlib import closing
import json
import logging
from pathlib import Path
import sqlite3
import threading


logger = logging.getLogger(__name__)


class AuditStore:
    """Append request/response records without exposing a public read interface.

    Schema creation is lazy so disabling auditing has no filesystem side effects.
    Connections belong to individual writes, making this safe to use from worker
    threads or multiple server processes sharing the application database.
    """

    def __init__(self, db_path: Path, enabled: bool = True):
        self.db_path = Path(db_path)
        self.enabled = enabled
        self._failure_lock = threading.Lock()
        self._failure_reported = False

    def write(self, record: dict) -> None:
        if not self.enabled:
            return

        encoded_record = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        routing = record.get("routing")
        selected = routing.get("selected") if isinstance(routing, dict) else None
        selected = selected if isinstance(selected, dict) else {}
        db_path = Path(self.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # The context manager for a connection commits/rolls back but does not
        # close it; closing() releases its file handle even when a write fails.
        with closing(sqlite3.connect(str(db_path), timeout=5.0)) as connection:
            with connection:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS internal_api_audit (
                        request_id TEXT PRIMARY KEY NOT NULL,
                        timestamp TEXT NOT NULL,
                        method TEXT NOT NULL,
                        path TEXT NOT NULL,
                        status_code INTEGER,
                        provider TEXT,
                        model TEXT,
                        record_json TEXT NOT NULL
                    )
                """)
                connection.execute("""
                    CREATE INDEX IF NOT EXISTS internal_api_audit_timestamp
                    ON internal_api_audit(timestamp)
                """)
                connection.execute("""
                    CREATE INDEX IF NOT EXISTS internal_api_audit_selected_route
                    ON internal_api_audit(provider, model)
                """)
                connection.execute("""
                    INSERT INTO internal_api_audit (
                        request_id, timestamp, method, path, status_code,
                        provider, model, record_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(request_id) DO NOTHING
                """, (
                    record["request_id"], record["timestamp"], record["method"],
                    record["path"], record.get("status_code"),
                    selected.get("provider"), selected.get("model"), encoded_record,
                ))

    def report_failure(self, exc: Exception) -> None:
        """Warn once without printing exception messages, paths, or audit data."""
        with self._failure_lock:
            if self._failure_reported:
                return
            self._failure_reported = True
            logger.warning(
                "Internal API audit persistence failed (%s); HTTP responses are unaffected.",
                type(exc).__name__,
            )
