"""Storage defaults and non-destructive migration of existing user data."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile

import pytest

from backend.app.config import Settings
from backend.app.storage import prepare_database


def test_default_database_is_in_system_temp(monkeypatch):
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("DATA_DIR", raising=False)
    settings = Settings(_env_file=None)
    assert settings.db_path == Path(tempfile.gettempdir()) / "the_connector" / "connector.db"
    assert settings.data_dir == settings.db_path.parent


def test_explicit_storage_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DB_PATH", raising=False)
    assert Settings(_env_file=None).db_path == tmp_path / "data" / "connector.db"
    monkeypatch.setenv("DB_PATH", str(tmp_path / "custom.db"))
    assert Settings(_env_file=None).db_path == tmp_path / "custom.db"


def test_new_storage_does_not_create_database_or_project_directory(tmp_path):
    destination = tmp_path / "system-temp" / "connector.db"
    legacy = tmp_path / "project" / "backend" / "data" / "connector.db"
    assert prepare_database(destination, legacy) is False
    assert destination.parent.is_dir()
    assert not destination.exists()
    assert not legacy.parent.exists()


def test_migration_preserves_wal_data_and_original(tmp_path):
    legacy = tmp_path / "old.db"
    destination = tmp_path / "system-temp" / "connector.db"
    with closing(sqlite3.connect(legacy)) as source:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("CREATE TABLE saved (name TEXT)")
        source.execute("INSERT INTO saved VALUES ('configuration and conversation')")
        source.commit()
        assert prepare_database(destination, legacy) is True
        assert source.execute("SELECT name FROM saved").fetchone() == ("configuration and conversation",)
        with closing(sqlite3.connect(destination)) as target:
            assert target.execute("SELECT name FROM saved").fetchone() == ("configuration and conversation",)
            target.execute("INSERT INTO saved VALUES ('new history')")
            target.commit()
        assert prepare_database(destination, legacy) is False
        with closing(sqlite3.connect(destination)) as target:
            assert target.execute("SELECT COUNT(*) FROM saved").fetchone()[0] == 2
    assert legacy.is_file()
    assert not list(destination.parent.glob(".connector-migration-*"))


def test_existing_database_is_never_replaced(tmp_path):
    destination = tmp_path / "current.db"
    destination.write_bytes(b"existing")
    legacy = tmp_path / "old.db"
    legacy.write_bytes(b"old")
    assert prepare_database(destination, legacy) is False
    assert destination.read_bytes() == b"existing"


def test_corrupt_source_leaves_no_partial_destination(tmp_path):
    legacy = tmp_path / "corrupt.db"
    legacy.write_bytes(b"not a SQLite database")
    destination = tmp_path / "system-temp" / "connector.db"
    with pytest.raises(sqlite3.DatabaseError):
        prepare_database(destination, legacy)
    assert not destination.exists()
    assert not list(destination.parent.glob(".connector-migration-*"))


def test_concurrent_destination_is_preserved(monkeypatch, tmp_path):
    legacy = tmp_path / "old.db"
    with closing(sqlite3.connect(legacy)) as source:
        source.execute("CREATE TABLE saved (name TEXT)")
    destination = tmp_path / "new.db"

    def publish_race(staging_path, target):
        target.write_bytes(b"another process won")
        raise FileExistsError()

    monkeypatch.setattr("backend.app.storage.os.link", publish_race)
    assert prepare_database(destination, legacy) is False
    assert destination.read_bytes() == b"another process won"
    assert not list(tmp_path.glob(".connector-migration-*"))
