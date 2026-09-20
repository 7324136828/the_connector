"""Retain normalized configuration snapshots independently of chats and models."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..schemas.configuration_history import ConfigurationHistoryCreate


class ConfigurationHistoryManager:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or settings.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS configuration_history (
                    id TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL UNIQUE,
                    config_json TEXT NOT NULL,
                    name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    first_loaded_at TEXT NOT NULL,
                    last_loaded_at TEXT NOT NULL,
                    load_count INTEGER NOT NULL CHECK (load_count > 0)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS configuration_history_references (
                    reference_key TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY (reference_key, content_hash)
                )
            """)
            conn.execute("BEGIN IMMEDIATE")
            self._backfill(conn)

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        record = dict(row)
        record["config"] = json.loads(record.pop("config_json"))
        record.pop("content_hash")
        return record

    def list_history(self) -> list[dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM configuration_history ORDER BY last_loaded_at DESC, rowid DESC"
            ).fetchall()
        return [self._record(row) for row in rows]

    def get_history(self, history_id: str) -> dict | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM configuration_history WHERE id = ?", (history_id,)
            ).fetchone()
        return self._record(row) if row is not None else None

    def remember(
        self, config: dict, name: str = "config.json", source: str = "editor", *,
        conn: sqlite3.Connection | None = None, reference_key: str | None = None,
        only_if_unseen: bool = False, first_loaded_at: str | None = None,
        last_loaded_at: str | None = None,
    ) -> dict:
        """Count a successful load; entity markers make imports and preservation idempotent.

        Passing the caller's connection keeps a session/library mutation and its
        history snapshot in the same transaction. The configuration and original
        name/source never change; only timestamps and the load count accumulate.
        """
        request = ConfigurationHistoryCreate(config=config, name=name, source=source)
        config_json = json.dumps(request.config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        content_hash = hashlib.sha256(config_json.encode("utf-8")).hexdigest()
        if conn is None:
            with self._get_conn() as owned_conn:
                owned_conn.execute("BEGIN IMMEDIATE")
                return self._remember(
                    owned_conn, request, config_json, content_hash, reference_key,
                    only_if_unseen, first_loaded_at, last_loaded_at,
                )
        return self._remember(
            conn, request, config_json, content_hash, reference_key,
            only_if_unseen, first_loaded_at, last_loaded_at,
        )

    def _remember(
        self, conn, request, config_json, content_hash, reference_key,
        only_if_unseen, first_loaded_at, last_loaded_at,
    ) -> dict:
        if only_if_unseen and reference_key and conn.execute(
            "SELECT 1 FROM configuration_history_references WHERE reference_key = ? AND content_hash = ?",
            (reference_key, content_hash),
        ).fetchone():
            return self._record(conn.execute(
                "SELECT * FROM configuration_history WHERE content_hash = ?", (content_hash,)
            ).fetchone())
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO configuration_history
               (id, content_hash, config_json, name, source, first_loaded_at, last_loaded_at, load_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(content_hash) DO UPDATE SET
                   first_loaded_at = MIN(first_loaded_at, excluded.first_loaded_at),
                   last_loaded_at = MAX(last_loaded_at, excluded.last_loaded_at),
                   load_count = load_count + 1""",
            (str(uuid.uuid4()), content_hash, config_json, request.name, request.source,
             first_loaded_at or now, last_loaded_at or now),
        )
        if reference_key:
            conn.execute(
                "INSERT OR IGNORE INTO configuration_history_references (reference_key, content_hash) VALUES (?, ?)",
                (reference_key, content_hash),
            )
        row = conn.execute(
            "SELECT * FROM configuration_history WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return self._record(row)

    def _backfill(self, conn: sqlite3.Connection) -> None:
        """Import all recoverable saved snapshots, including closed sessions, once."""
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "configurations" in tables:
            for row in conn.execute("SELECT * FROM configurations").fetchall():
                try:
                    self.remember(
                        json.loads(row["config_json"]),
                        name=(row["name"] or "config.json").strip()[:200] or "config.json",
                        source="library",
                        conn=conn, reference_key=f"library:{row['id']}", only_if_unseen=True,
                        first_loaded_at=row["created_at"], last_loaded_at=row["updated_at"],
                    )
                except (ValueError, TypeError):
                    # Malformed legacy data must not prevent startup or enter history.
                    continue
        if "sessions" in tables:
            for row in conn.execute("SELECT * FROM sessions").fetchall():
                try:
                    if row["config_json"]:
                        config = json.loads(row["config_json"])
                        if not isinstance(config, dict):
                            continue
                    elif row["provider"] not in {"custom_sequence", "sequence", "config"}:
                        config = {"sequences": [{
                            "provider": row["provider"], "model": row["model"], "retries": 0,
                        }]}
                    else:
                        continue
                    config.setdefault("system_prompt", row["system_prompt"])
                    config.setdefault("past_memory", bool(row["past_memory"]))
                    config.setdefault("context_window", row["context_window"])
                    self.remember(
                        config, name=(row["title"] or "config.json").strip()[:200] or "config.json",
                        source="session",
                        conn=conn, reference_key=f"session:{row['id']}", only_if_unseen=True,
                        first_loaded_at=row["created_at"], last_loaded_at=row["updated_at"],
                    )
                except (ValueError, TypeError):
                    continue


configuration_history_manager = ConfigurationHistoryManager()
