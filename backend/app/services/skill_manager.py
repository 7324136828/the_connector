"""Persistent Python skills created from conversation-derived code."""

from __future__ import annotations

import ast
import json
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings


SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
RESERVED_SKILL_NAMES = {"run_python_script", "create_skill_from_conversation"}
MAX_SKILL_CODE_BYTES = 100_000
MAX_CONVERSATION_BYTES = 1_000_000


class SkillValidationError(ValueError):
    """A skill cannot be stored or executed as supplied."""


class SkillNotFoundError(ValueError):
    """A requested skill does not exist."""


class DuplicateSkillNameError(ValueError):
    """A skill name is already in use."""


def validate_skill_code(code: str) -> str:
    """Validate the Python skill contract without executing the code."""
    code = code.strip()
    if not code:
        raise SkillValidationError("python_code must not be empty.")
    if len(code.encode("utf-8")) > MAX_SKILL_CODE_BYTES:
        raise SkillValidationError(f"python_code must be at most {MAX_SKILL_CODE_BYTES} bytes.")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise SkillValidationError(f"python_code is invalid: {exc.msg} (line {exc.lineno}).") from exc
    if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run" for node in tree.body):
        raise SkillValidationError("python_code must define a top-level run(args) function.")
    return code


def run_skill_code(code: str, arguments: dict[str, Any], timeout: float = 5.0) -> Any:
    """Execute a stored skill in a separate isolated-mode Python process."""
    wrapper = r'''
import contextlib
import io
import json
import sys

payload = json.load(sys.stdin)
namespace = {"__name__": "__connector_skill__"}
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    exec(compile(payload["code"], "<stored-skill>", "exec"), namespace, namespace)
    function = namespace.get("run")
    if not callable(function):
        raise TypeError("Stored skill does not define callable run(args).")
    result = function(payload["arguments"])
json.dump({"result": result, "stdout": captured.getvalue()}, sys.stdout, default=str)
'''
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", wrapper],
            input=json.dumps({"code": code, "arguments": arguments}),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Skill execution timed out after {timeout:.1f} seconds.") from exc
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout or "Unknown Python error").strip()
        raise RuntimeError(f"Skill execution failed: {error}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Skill returned an invalid execution envelope.") from exc
    if payload.get("stdout"):
        return {"result": payload.get("result"), "stdout": payload["stdout"]}
    return payload.get("result")


class SkillManager:
    """Store skills in SQLite and expose their executable definitions."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or settings.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS skills (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    python_code TEXT NOT NULL,
                    source_conversation TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["parameters"] = json.loads(record.pop("parameters_json"))
        return record

    @staticmethod
    def _validate(
        name: str,
        description: str,
        parameters: dict[str, Any],
        python_code: str,
        source_conversation: str,
    ) -> tuple[str, str, dict[str, Any], str, str]:
        name = name.strip()
        description = description.strip()
        source_conversation = source_conversation.strip()
        if not SKILL_NAME_PATTERN.fullmatch(name):
            raise SkillValidationError(
                "name must be 2-64 lowercase letters, numbers, or underscores and start with a letter."
            )
        if name in RESERVED_SKILL_NAMES:
            raise SkillValidationError(f"'{name}' is reserved for a native skill.")
        if not description:
            raise SkillValidationError("description must not be empty.")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise SkillValidationError("parameters must be a JSON Schema object with type='object'.")
        if len(source_conversation.encode("utf-8")) > MAX_CONVERSATION_BYTES:
            raise SkillValidationError(
                f"source_conversation must be at most {MAX_CONVERSATION_BYTES} bytes."
            )
        return name, description, parameters, validate_skill_code(python_code), source_conversation

    def create_skill(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        python_code: str,
        source_conversation: str,
    ) -> dict[str, Any]:
        name, description, parameters, python_code, source_conversation = self._validate(
            name, description, parameters, python_code, source_conversation,
        )
        skill_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT INTO skills "
                    "(id, name, description, parameters_json, python_code, source_conversation, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        skill_id, name, description, json.dumps(parameters), python_code,
                        source_conversation, now, now,
                    ),
                )
                row = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            if "skills.name" in str(exc):
                raise DuplicateSkillNameError(f"Skill '{name}' already exists.") from exc
            raise
        return self._record(row)

    def list_skills(self) -> list[dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM skills ORDER BY created_at, id").fetchall()
        return [self._record(row) for row in rows]

    def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
        return self._record(row) if row is not None else None

    def get_skill_by_name(self, name: str) -> dict[str, Any] | None:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM skills WHERE name = ?", (name,)).fetchone()
        return self._record(row) if row is not None else None

    def update_skill(self, skill_id: str, **changes: Any) -> dict[str, Any]:
        """Validate and update selected skill fields atomically."""
        allowed = {"name", "description", "parameters", "python_code", "source_conversation"}
        unknown = set(changes) - allowed
        if unknown:
            raise SkillValidationError("Unsupported skill fields: " + ", ".join(sorted(unknown)) + ".")
        with self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
            if row is None:
                raise SkillNotFoundError("Skill not found.")
            current = self._record(row)
            values = {
                key: changes.get(key, current[key])
                for key in allowed
            }
            name, description, parameters, python_code, source_conversation = self._validate(
                values["name"], values["description"], values["parameters"],
                values["python_code"], values["source_conversation"],
            )
            now = datetime.now(timezone.utc).isoformat()
            try:
                conn.execute(
                    "UPDATE skills SET name = ?, description = ?, parameters_json = ?, "
                    "python_code = ?, source_conversation = ?, updated_at = ? WHERE id = ?",
                    (
                        name, description, json.dumps(parameters), python_code,
                        source_conversation, now, skill_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "skills.name" in str(exc):
                    raise DuplicateSkillNameError(f"Skill '{name}' already exists.") from exc
                raise
            updated = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
        return self._record(updated)

    def delete_skill(self, skill_id: str) -> dict[str, Any]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
            if row is None:
                raise SkillNotFoundError("Skill not found.")
            conn.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
        return self._record(row)

    def execute_skill(self, name: str, arguments: dict[str, Any]) -> Any:
        record = self.get_skill_by_name(name)
        if record is None:
            raise SkillNotFoundError(f"Skill '{name}' not found.")
        return run_skill_code(record["python_code"], arguments)


skill_manager = SkillManager()
