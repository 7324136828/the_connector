"""Persistent, typed coding skills created from conversation-derived code."""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings


SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
RESERVED_SKILL_NAMES = {
    "run_python_script",
    "install_python_package",
    "create_coding_skill_from_conversation",
    "fetch_memory",
}
SKILL_TYPES = frozenset({"python", "cmd", "c++"})
MAX_SKILL_CODE_BYTES = 100_000
MAX_CONVERSATION_BYTES = 1_000_000


class SkillValidationError(ValueError):
    """A skill cannot be stored or executed as supplied."""


class SkillNotFoundError(ValueError):
    """A requested skill does not exist."""


class DuplicateSkillNameError(ValueError):
    """A skill name is already in use."""


def detect_skill_type(code: str) -> str:
    """Infer a supported runtime from source code when no type was supplied."""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        tree = None
    if tree is not None and any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"
        for node in tree.body
    ):
        return "python"

    if re.search(r"#\s*include\s*[<\"]", code) or re.search(
        r"\b(?:int|auto)\s+main\s*\(", code,
    ):
        return "c++"

    if re.search(
        r"(?im)^\s*(?:@echo\s+off|set\s+|if\s+|for\s+|call\s+|echo\s+|rem\s+|::)",
        code,
    ) or "%CONNECTOR_SKILL_ARGS%" in code:
        return "cmd"

    raise SkillValidationError(
        "Could not infer the coding skill type. Set type to 'python', 'cmd', or 'c++'."
    )


def validate_skill_code(code: str, skill_type: str | None = None) -> tuple[str, str]:
    """Validate source for its selected or automatically detected runtime."""
    code = code.strip()
    if not code:
        raise SkillValidationError("python_code must not be empty.")
    if len(code.encode("utf-8")) > MAX_SKILL_CODE_BYTES:
        raise SkillValidationError(f"python_code must be at most {MAX_SKILL_CODE_BYTES} bytes.")
    normalized_type = skill_type.strip().lower() if isinstance(skill_type, str) else ""
    if normalized_type and normalized_type not in SKILL_TYPES:
        raise SkillValidationError("type must be 'python', 'cmd', or 'c++'.")
    normalized_type = normalized_type or detect_skill_type(code)

    if normalized_type == "python":
        try:
            tree = ast.parse(code, mode="exec")
        except SyntaxError as exc:
            raise SkillValidationError(
                f"python_code is invalid: {exc.msg} (line {exc.lineno})."
            ) from exc
        if not any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"
            for node in tree.body
        ):
            raise SkillValidationError("Python skill code must define a top-level run(args) function.")
    elif normalized_type == "c++" and not re.search(
        r"\b(?:int|auto)\s+main\s*\(", code,
    ):
        raise SkillValidationError("C++ skill code must define main().")

    return normalized_type, code


def _runtime_environment(python_executable: str | None) -> dict[str, str]:
    environment = os.environ.copy()
    arguments_path = ""
    if python_executable:
        python_dir = str(Path(python_executable).resolve().parent)
        environment["PATH"] = python_dir + os.pathsep + environment.get("PATH", "")
        environment["CONNECTOR_PYTHON_EXECUTABLE"] = python_executable
    environment.setdefault("CONNECTOR_SKILL_ARGS_FILE", arguments_path)
    return environment


def _external_skill_result(completed: subprocess.CompletedProcess[str], runtime: str) -> Any:
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout or f"Unknown {runtime} error").strip()
        raise RuntimeError(f"{runtime} skill execution failed: {error}")
    output = (completed.stdout or "").strip()
    if not output:
        return {"exit_code": 0}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return output


def run_cmd_skill(
    code: str,
    arguments: dict[str, Any],
    timeout: float,
    python_executable: str | None,
) -> Any:
    """Run a Windows CMD skill with JSON arguments on stdin and in the environment."""
    command_interpreter = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
    if not command_interpreter:
        raise RuntimeError("CMD skills require Windows cmd.exe, which was not found.")
    argument_json = json.dumps(arguments, ensure_ascii=False)
    with tempfile.TemporaryDirectory(prefix="connector-cmd-skill-") as temp_dir:
        temp_path = Path(temp_dir)
        script_path = temp_path / "skill.cmd"
        arguments_path = temp_path / "arguments.json"
        script_path.write_text(code, encoding="utf-8")
        arguments_path.write_text(argument_json, encoding="utf-8")
        environment = _runtime_environment(python_executable)
        environment["CONNECTOR_SKILL_ARGS"] = argument_json
        environment["CONNECTOR_SKILL_ARGS_FILE"] = str(arguments_path)
        try:
            completed = subprocess.run(
                [command_interpreter, "/d", "/s", "/c", str(script_path)],
                input=argument_json,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=environment,
                cwd=temp_dir,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"CMD skill execution timed out after {timeout:.1f} seconds.") from exc
    return _external_skill_result(completed, "CMD")


def discover_cpp_compiler() -> tuple[str, str] | None:
    """Return the first supported local C++ compiler kind and executable."""
    for kind, executable_name in (("g++", "g++"), ("clang++", "clang++"), ("cl", "cl.exe")):
        executable = shutil.which(executable_name)
        if executable:
            return kind, executable
    return None


def run_cpp_skill(
    code: str,
    arguments: dict[str, Any],
    timeout: float,
    python_executable: str | None,
) -> Any:
    """Compile a C++17 skill locally, then pass JSON arguments to it on stdin."""
    compiler = discover_cpp_compiler()
    if compiler is None:
        raise RuntimeError("C++ skills require a local g++, clang++, or MSVC cl compiler.")
    compiler_kind, compiler_executable = compiler
    with tempfile.TemporaryDirectory(prefix="connector-cpp-skill-") as temp_dir:
        temp_path = Path(temp_dir)
        source_path = temp_path / "skill.cpp"
        executable_path = temp_path / ("skill.exe" if os.name == "nt" else "skill")
        source_path.write_text(code, encoding="utf-8")
        if compiler_kind == "cl":
            compile_command = [
                compiler_executable, "/nologo", "/std:c++17", "/EHsc",
                str(source_path), f"/Fe:{executable_path}",
            ]
        else:
            compile_command = [
                compiler_executable, "-std=c++17", "-O2", str(source_path),
                "-o", str(executable_path),
            ]
        try:
            compiled = subprocess.run(
                compile_command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(30.0, timeout),
                cwd=temp_dir,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("C++ skill compilation timed out.") from exc
        if compiled.returncode != 0:
            error = (compiled.stderr or compiled.stdout or "Unknown compiler error").strip()
            raise RuntimeError(f"C++ skill compilation failed: {error}")

        argument_json = json.dumps(arguments, ensure_ascii=False)
        environment = _runtime_environment(python_executable)
        environment["CONNECTOR_SKILL_ARGS"] = argument_json
        try:
            completed = subprocess.run(
                [str(executable_path)],
                input=argument_json,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=environment,
                cwd=temp_dir,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"C++ skill execution timed out after {timeout:.1f} seconds.") from exc
    return _external_skill_result(completed, "C++")


def run_skill_code(
    code: str,
    arguments: dict[str, Any],
    timeout: float = 5.0,
    python_executable: str | None = None,
    skill_type: str = "python",
) -> Any:
    """Execute a typed stored skill in an isolated child process."""
    if skill_type == "cmd":
        return run_cmd_skill(code, arguments, timeout, python_executable)
    if skill_type == "c++":
        return run_cpp_skill(code, arguments, timeout, python_executable)
    if skill_type != "python":
        raise SkillValidationError("type must be 'python', 'cmd', or 'c++'.")
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
            [python_executable or sys.executable, "-I", "-c", wrapper],
            input=json.dumps({"code": code, "arguments": arguments}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
                    type TEXT NOT NULL DEFAULT 'python',
                    source_conversation TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(skills)")}
            if "type" not in columns:
                conn.execute(
                    "ALTER TABLE skills ADD COLUMN type TEXT NOT NULL DEFAULT 'python'"
                )

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
        type: str | None = None,
    ) -> tuple[str, str, dict[str, Any], str, str, str]:
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
        skill_type, python_code = validate_skill_code(python_code, type)
        return name, description, parameters, python_code, source_conversation, skill_type

    def create_skill(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        python_code: str,
        source_conversation: str,
        type: str | None = None,
    ) -> dict[str, Any]:
        name, description, parameters, python_code, source_conversation, skill_type = self._validate(
            name, description, parameters, python_code, source_conversation, type,
        )
        skill_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT INTO skills "
                    "(id, name, description, parameters_json, python_code, type, source_conversation, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        skill_id, name, description, json.dumps(parameters), python_code,
                        skill_type, source_conversation, now, now,
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
        allowed = {"name", "description", "parameters", "python_code", "source_conversation", "type"}
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
            name, description, parameters, python_code, source_conversation, skill_type = self._validate(
                values["name"], values["description"], values["parameters"],
                values["python_code"], values["source_conversation"], values["type"],
            )
            now = datetime.now(timezone.utc).isoformat()
            try:
                conn.execute(
                    "UPDATE skills SET name = ?, description = ?, parameters_json = ?, "
                    "python_code = ?, type = ?, source_conversation = ?, updated_at = ? WHERE id = ?",
                    (
                        name, description, json.dumps(parameters), python_code,
                        skill_type, source_conversation, now, skill_id,
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

    def export_skills(self) -> dict[str, Any]:
        """Return a portable, versioned representation without database IDs."""
        fields = ("name", "description", "parameters", "python_code", "type", "source_conversation")
        return {
            "version": 1,
            "skills": [
                {field: record[field] for field in fields}
                for record in self.list_skills()
            ],
        }

    def import_skills(
        self,
        records: list[dict[str, Any]],
        *,
        conflict: str = "error",
    ) -> dict[str, Any]:
        """Atomically import portable skills with explicit name-conflict behavior."""
        if conflict not in {"error", "skip", "replace"}:
            raise SkillValidationError("conflict must be 'error', 'skip', or 'replace'.")
        validated = []
        seen_names: set[str] = set()
        for record in records:
            values = self._validate(
                record.get("name", ""),
                record.get("description", ""),
                record.get("parameters", {}),
                record.get("python_code", ""),
                record.get("source_conversation", ""),
                record.get("type"),
            )
            if values[0] in seen_names:
                raise DuplicateSkillNameError(
                    f"Skill '{values[0]}' appears more than once in the import."
                )
            seen_names.add(values[0])
            validated.append(values)

        counts = {"created": 0, "replaced": 0, "skipped": 0}
        affected_ids: list[str] = []
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for name, description, parameters, code, source, skill_type in validated:
                existing = conn.execute(
                    "SELECT id FROM skills WHERE name = ?", (name,),
                ).fetchone()
                if existing is not None:
                    if conflict == "error":
                        raise DuplicateSkillNameError(f"Skill '{name}' already exists.")
                    if conflict == "skip":
                        counts["skipped"] += 1
                        continue
                    conn.execute(
                        "UPDATE skills SET description = ?, parameters_json = ?, python_code = ?, "
                        "type = ?, source_conversation = ?, updated_at = ? WHERE id = ?",
                        (
                            description, json.dumps(parameters), code, skill_type,
                            source, now, existing["id"],
                        ),
                    )
                    affected_ids.append(existing["id"])
                    counts["replaced"] += 1
                    continue

                skill_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO skills "
                    "(id, name, description, parameters_json, python_code, type, "
                    "source_conversation, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        skill_id, name, description, json.dumps(parameters), code,
                        skill_type, source, now, now,
                    ),
                )
                affected_ids.append(skill_id)
                counts["created"] += 1
            imported_rows = [
                conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
                for skill_id in affected_ids
            ]
        return {
            **counts,
            "skills": [self._record(row) for row in imported_rows if row is not None],
        }

    def execute_skill(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        python_executable: str | None = None,
    ) -> Any:
        record = self.get_skill_by_name(name)
        if record is None:
            raise SkillNotFoundError(f"Skill '{name}' not found.")
        return run_skill_code(
            record["python_code"], arguments, python_executable=python_executable,
            skill_type=record["type"],
        )


skill_manager = SkillManager()
