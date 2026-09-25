"""Internal HTTP audit persistence, separate from user and LLM-visible data."""

from contextlib import closing
import json
import logging
from pathlib import Path
import sqlite3
import threading


logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 5


class AuditStore:
    """Append request/response records without exposing a public read interface.

    Schema creation is lazy so disabling auditing has no filesystem side effects.
    Connections belong to individual writes, making this safe to use from worker
    threads or multiple server processes sharing the application database.
    """

    def __init__(self, db_path: Path, enabled: bool = True,
                 max_bytes: int = DEFAULT_MAX_BYTES,
                 max_entries: int = DEFAULT_MAX_ENTRIES):
        self.db_path = Path(db_path)
        self.enabled = enabled
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._failure_lock = threading.Lock()
        self._failure_reported = False

    def _prune(self, connection: sqlite3.Connection) -> int:
        """Delete oldest rows until both retention limits are satisfied."""
        changes_before = connection.total_changes
        connection.execute("""
            DELETE FROM internal_api_audit
            WHERE rowid IN (
                SELECT rowid
                FROM internal_api_audit
                ORDER BY timestamp DESC, rowid DESC
                LIMIT -1 OFFSET ?
            )
        """, (self.max_entries,))
        rows = connection.execute("""
            SELECT rowid, length(CAST(record_json AS BLOB))
            FROM internal_api_audit
            ORDER BY timestamp DESC, rowid DESC
        """).fetchall()

        retained_bytes = 0
        delete_rowids = []
        for position, (rowid, record_bytes) in enumerate(rows):
            # Always retain the newest record. A single complete record may be
            # larger than max_bytes, just as one JSONL record may exceed the
            # file rotation target.
            within_count = position < self.max_entries
            within_bytes = position == 0 or retained_bytes + record_bytes <= self.max_bytes
            if within_count and within_bytes:
                retained_bytes += record_bytes
            else:
                delete_rowids.append(rowid)

        if delete_rowids:
            connection.executemany(
                "DELETE FROM internal_api_audit WHERE rowid = ?",
                ((rowid,) for rowid in delete_rowids),
            )
        return connection.total_changes - changes_before

    def _compact_if_worthwhile(self, connection: sqlite3.Connection) -> None:
        """Reclaim disk after a large prune, while avoiding a VACUUM per write."""
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        free_pages = connection.execute("PRAGMA freelist_count").fetchone()[0]
        free_bytes = free_pages * page_size
        if free_bytes >= self.max_bytes and free_pages * 2 >= page_count:
            connection.execute("VACUUM")

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
            pruned = 0
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
                pruned = self._prune(connection)
            if pruned:
                self._compact_if_worthwhile(connection)

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
