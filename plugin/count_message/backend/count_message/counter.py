"""Count retained transcript rows without copying conversation text."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def default_database_path() -> Path:
    """Match Connector's environment overrides (the host .env is not loaded)."""
    if os.environ.get("DB_PATH"):
        return Path(os.environ["DB_PATH"])
    return Path(os.environ.get("DATA_DIR") or Path(tempfile.gettempdir()) / "the_connector") / "connector.db"


def count_messages(
    db_path: str | Path | None = None,
    session_id: str | None = None,
    include_system_sessions: bool = False,
) -> dict:
    """Count nonblank user/assistant messages in active and closed sessions.

    System sessions are API-created sessions with user_session=0. A requested
    session still follows the include_system_sessions policy. Missing sessions
    raise LookupError; a missing/uninitialized database raises sqlite3.Error.
    """
    if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
        raise ValueError("session_id must be a nonblank string or null")
    if not isinstance(include_system_sessions, bool):
        raise ValueError("include_system_sessions must be a boolean")
    path = Path(db_path) if db_path is not None else default_database_path()
    # mode=ro prevents accidentally creating a new database or changing history.
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.create_function("has_text", 1, lambda content: isinstance(content, str) and bool(content.strip()))
        conn.execute("BEGIN")
        if session_id is not None and conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone() is None:
            raise LookupError("Session not found")
        conditions = []
        params = []
        if session_id is not None:
            conditions.append("s.id = ?")
            params.append(session_id)
        if not include_system_sessions:
            conditions.append("s.user_session = 1")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        # Count empty sessions as part of the selected scope, but not as messages.
        session_count = conn.execute("SELECT COUNT(*) FROM sessions s" + where, params).fetchone()[0]
        text_where = where + (" AND " if where else " WHERE ") + "m.role IN ('user', 'assistant') AND has_text(m.content)"
        user, assistant = conn.execute(
            "SELECT COALESCE(SUM(m.role = 'user'), 0), COALESCE(SUM(m.role = 'assistant'), 0) "
            "FROM messages m JOIN sessions s ON s.id = m.session_id" + text_where, params,
        ).fetchone()
        return {
            "plugin": "count_message",
            "session_id": session_id,
            "include_system_sessions": include_system_sessions,
            "sessions": session_count,
            "user_messages": user,
            "assistant_messages": assistant,
            "total_messages": user + assistant,
            "counted_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        conn.close()
