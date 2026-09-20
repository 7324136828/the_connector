"""Prepare persistent SQLite storage without overwriting an existing database."""

from contextlib import closing
import logging
import os
from pathlib import Path
import sqlite3
import tempfile

logger = logging.getLogger(__name__)


def prepare_database(db_path: Path, legacy_path: Path | None = None) -> bool:
    """Copy a legacy project database once, preserving committed WAL data.

    Publish a complete SQLite backup atomically, without replacing a database
    another process may have created. The original stays intact as a backup.
    Returns whether this process migrated the database.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists() or legacy_path is None or not Path(legacy_path).is_file():
        return False

    legacy_path = Path(legacy_path).resolve()
    descriptor, staging_name = tempfile.mkstemp(
        prefix=".connector-migration-", suffix=".db", dir=db_path.parent,
    )
    os.close(descriptor)
    staging_path = Path(staging_name)
    try:
        with closing(sqlite3.connect(legacy_path.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(staging_path)) as destination:
                source.backup(destination)
                check = destination.execute("PRAGMA quick_check").fetchone()
                if check != ("ok",):
                    raise sqlite3.DatabaseError("Legacy database failed its integrity check.")
        try:
            # Hard-link publication is atomic and fails if the target exists.
            # Both files are in the same local temp directory/filesystem.
            os.link(staging_path, db_path)
        except FileExistsError:
            return False
        logger.info("Copied database from %s to %s; original preserved.", legacy_path, db_path)
        return True
    finally:
        staging_path.unlink(missing_ok=True)
