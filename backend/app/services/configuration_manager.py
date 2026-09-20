"""Persistent named routing configurations, independent of session snapshots."""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..schemas.library import ConfigurationCreate, ConfigurationUpdate
from .configuration_history import ConfigurationHistoryManager


class DuplicateModelIdError(ValueError):
    """A model ID is already assigned to another saved configuration."""


class ConfigurationNotFoundError(ValueError):
    """The requested saved configuration does not exist."""


class ConfigurationManager:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or settings.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS configurations (
                    id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    context_length INTEGER CHECK (context_length > 0),
                    config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(configurations)")}
            if "context_length" not in columns:
                conn.execute("ALTER TABLE configurations ADD COLUMN context_length INTEGER CHECK (context_length > 0)")
        self.configuration_history = ConfigurationHistoryManager(self.db_path)

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        record = dict(row)
        record["config"] = json.loads(record.pop("config_json"))
        record["active"] = bool(record["active"])
        return record

    def list_configs(self, active_only: bool = False) -> list[dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM configurations WHERE (? = 0 OR active = 1) "
                "ORDER BY created_at, id", (int(active_only),),
            ).fetchall()
        return [self._record(row) for row in rows]

    def get_config(self, config_id: str) -> dict | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM configurations WHERE id = ?", (config_id,),
            ).fetchone()
        return self._record(row) if row is not None else None

    def get_active_model(self, model_id: str) -> dict | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM configurations WHERE model_id = ? AND active = 1",
                (model_id,),
            ).fetchone()
        return self._record(row) if row is not None else None

    def create_config(
        self, name: str, model_id: str, config: dict,
        active: bool = True, description: str = "",
        context_length: int | None = None,
    ) -> dict:
        request = ConfigurationCreate(
            name=name, model_id=model_id, config=config,
            active=active, description=description,
            context_length=context_length,
        )
        config_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT INTO configurations "
                    "(id, model_id, name, description, active, context_length, config_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (config_id, request.model_id, request.name, request.description,
                     int(request.active), request.context_length, json.dumps(request.config), now, now),
                )
                row = conn.execute(
                    "SELECT * FROM configurations WHERE id = ?", (config_id,),
                ).fetchone()
                self.configuration_history.remember(
                    request.config, name=request.name, source="library", conn=conn,
                    reference_key=f"library:{config_id}",
                )
        except sqlite3.IntegrityError as exc:
            if "configurations.model_id" in str(exc):
                raise DuplicateModelIdError(
                    f"Model ID '{request.model_id}' already exists."
                ) from exc
            raise
        return self._record(row)

    def update_config(self, config_id: str, **changes) -> dict:
        request = ConfigurationUpdate(**changes)
        values = request.model_dump(exclude_unset=True)
        # Only validated, fixed column names enter the query. All values are bound.
        columns = {"name": "name", "description": "description", "active": "active", "context_length": "context_length", "config": "config_json"}
        with self._get_conn() as conn:
            # Serialize read/update so deletion cannot occur between the two.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM configurations WHERE id = ?", (config_id,),
            ).fetchone()
            if row is None:
                raise ConfigurationNotFoundError("Configuration not found.")
            if "config" in values:
                # Preserve a legacy snapshot before replacing its only live copy.
                try:
                    self.configuration_history.remember(
                        json.loads(row["config_json"]), name=row["name"], source="library",
                        conn=conn, reference_key=f"library:{config_id}", only_if_unseen=True,
                        first_loaded_at=row["created_at"], last_loaded_at=row["updated_at"],
                    )
                except (ValueError, TypeError):
                    pass
            if values:
                assignments = [f"{columns[key]} = ?" for key in values]
                parameters = [
                    json.dumps(value) if key == "config" else int(value) if key == "active" else value
                    for key, value in values.items()
                ]
                assignments.append("updated_at = ?")
                parameters.extend([datetime.now(timezone.utc).isoformat(), config_id])
                conn.execute(
                    f"UPDATE configurations SET {', '.join(assignments)} WHERE id = ?",
                    parameters,
                )
                row = conn.execute(
                    "SELECT * FROM configurations WHERE id = ?", (config_id,),
                ).fetchone()
                if "config" in values:
                    self.configuration_history.remember(
                        values["config"], name=row["name"], source="library", conn=conn,
                        reference_key=f"library:{config_id}",
                    )
        return self._record(row)

    def delete_config(self, config_id: str) -> bool:
        with self._get_conn() as conn:
            result = conn.execute("DELETE FROM configurations WHERE id = ?", (config_id,))
        return result.rowcount > 0


configuration_manager = ConfigurationManager()
