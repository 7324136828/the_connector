"""Managed virtual environments for agentic Python execution."""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings


ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")


class PythonEnvironmentError(ValueError):
    """A managed Python environment operation could not be completed."""


class PythonEnvironmentNotFoundError(PythonEnvironmentError):
    """The requested managed Python environment does not exist."""


class DuplicatePythonEnvironmentError(PythonEnvironmentError):
    """A managed Python environment already uses the requested name."""


class PythonEnvironmentProvisionError(RuntimeError):
    """A virtual environment could not be created or repaired."""


def python_executable_for(environment_dir: Path) -> Path:
    """Return the platform-specific Python executable inside a virtual environment."""
    if sys.platform == "win32":
        return environment_dir / "Scripts" / "python.exe"
    return environment_dir / "bin" / "python"


class PythonEnvironmentManager:
    """Create, persist, select, and resolve managed execution environments."""

    def __init__(
        self,
        db_path: Path | None = None,
        default_environment_dir: Path | None = None,
        managed_environments_dir: Path | None = None,
    ):
        self.db_path = Path(db_path or settings.db_path)
        self.default_environment_dir = Path(
            default_environment_dir or settings.agent_python_env_dir
        )
        self.managed_environments_dir = Path(
            managed_environments_dir or settings.agent_python_envs_dir
        )
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS python_environments (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    path TEXT NOT NULL UNIQUE,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    selected INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)
            selected_exists = conn.execute(
                "SELECT 1 FROM python_environments WHERE selected = 1 LIMIT 1"
            ).fetchone() is not None
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO python_environments "
                "(id, name, path, is_default, selected, created_at) VALUES (?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name = excluded.name, path = excluded.path, is_default = 1",
                (
                    "default",
                    "Default",
                    str(self.default_environment_dir),
                    0 if selected_exists else 1,
                    now,
                ),
            )
            if not selected_exists:
                conn.execute("UPDATE python_environments SET selected = (id = 'default')")

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["is_default"] = bool(record["is_default"])
        record["selected"] = bool(record["selected"])
        record["ready"] = python_executable_for(Path(record["path"])).is_file()
        return record

    @staticmethod
    def _validate_name(name: str) -> str:
        name = name.strip() if isinstance(name, str) else ""
        if not ENVIRONMENT_NAME_PATTERN.fullmatch(name):
            raise PythonEnvironmentError(
                "name must be 1-64 characters and contain only letters, numbers, spaces, '.', '_', or '-'."
            )
        return name

    @staticmethod
    def _slug(name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        return slug or "environment"

    def list_environments(self) -> list[dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM python_environments ORDER BY is_default DESC, created_at, name"
            ).fetchall()
        return [self._record(row) for row in rows]

    def get_environment(self, environment_id: str) -> dict[str, Any] | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM python_environments WHERE id = ?", (environment_id,)
            ).fetchone()
        return self._record(row) if row is not None else None

    def selected_environment(self) -> dict[str, Any]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM python_environments WHERE selected = 1 "
                "ORDER BY is_default DESC LIMIT 1"
            ).fetchone()
        if row is None:
            raise PythonEnvironmentError("No Python execution environment is selected.")
        return self._record(row)

    def _ensure_environment(self, environment: dict[str, Any]) -> str:
        environment_dir = Path(environment["path"])
        python_executable = python_executable_for(environment_dir)
        if python_executable.is_file():
            return str(python_executable)

        with self._lock:
            if python_executable.is_file():
                return str(python_executable)
            try:
                completed = subprocess.run(
                    [sys.executable, "-m", "venv", str(environment_dir)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=180.0,
                )
            except subprocess.TimeoutExpired as exc:
                raise PythonEnvironmentProvisionError(
                    f"Creating Python environment '{environment['name']}' timed out."
                ) from exc
            if completed.returncode != 0 or not python_executable.is_file():
                output = "\n".join(
                    part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
                )
                raise PythonEnvironmentProvisionError(
                    f"Could not create Python environment '{environment['name']}' at '{environment_dir}'."
                    + (f"\n{output[-4000:]}" if output else "")
                )
        return str(python_executable)

    def create_environment(self, name: str, *, select: bool = True) -> dict[str, Any]:
        name = self._validate_name(name)
        with self._lock:
            with self._get_conn() as conn:
                duplicate = conn.execute(
                    "SELECT 1 FROM python_environments WHERE name = ? COLLATE NOCASE", (name,)
                ).fetchone()
            if duplicate is not None:
                raise DuplicatePythonEnvironmentError(
                    f"Python environment '{name}' already exists."
                )

            environment_id = str(uuid.uuid4())
            environment_dir = self.managed_environments_dir / (
                f"{self._slug(name)}-{environment_id[:8]}"
            )
            provisional = {"name": name, "path": str(environment_dir)}
            self._ensure_environment(provisional)
            now = datetime.now(timezone.utc).isoformat()
            try:
                with self._get_conn() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    if select:
                        conn.execute("UPDATE python_environments SET selected = 0")
                    conn.execute(
                        "INSERT INTO python_environments "
                        "(id, name, path, is_default, selected, created_at) "
                        "VALUES (?, ?, ?, 0, ?, ?)",
                        (environment_id, name, str(environment_dir), int(select), now),
                    )
                    row = conn.execute(
                        "SELECT * FROM python_environments WHERE id = ?", (environment_id,)
                    ).fetchone()
            except sqlite3.IntegrityError as exc:
                raise DuplicatePythonEnvironmentError(
                    f"Python environment '{name}' already exists."
                ) from exc
        return self._record(row)

    def select_environment(self, environment_id: str) -> dict[str, Any]:
        with self._lock:
            environment = self.get_environment(environment_id)
            if environment is None:
                raise PythonEnvironmentNotFoundError("Python environment not found.")
            self._ensure_environment(environment)
            with self._get_conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE python_environments SET selected = 0")
                conn.execute(
                    "UPDATE python_environments SET selected = 1 WHERE id = ?",
                    (environment_id,),
                )
                row = conn.execute(
                    "SELECT * FROM python_environments WHERE id = ?", (environment_id,)
                ).fetchone()
        return self._record(row)

    def current_python_executable(self) -> str:
        return self._ensure_environment(self.selected_environment())


python_environment_manager = PythonEnvironmentManager()
