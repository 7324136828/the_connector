"""Internal audit data stays durable and isolated from chat/memory tables."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
import logging
import sqlite3

import pytest

from backend.app.audit_store import AuditStore


def audit_record(request_id="request-1"):
    return {
        "request_id": request_id,
        "timestamp": "2026-09-20T14:00:00+00:00",
        "method": "POST",
        "path": "/api/chat/completions",
        "route": "/api/chat/completions",
        "status_code": 200,
        "content_type": "application/json",
        "duration_ms": 12.125,
        "body": {"choices": [{"message": {"content": "A saved reply: café, 日本語, 🧪"}}]},
        "body_bytes": 111,
        "captured_bytes": 111,
        "truncated": False,
        "complete": True,
        "request": {
            "body": {"model": "research", "messages": [{"role": "user", "content": "Remember my question"}]},
            "query": "trace=1&label=%E6%97%A5",
            "content_type": "application/json",
            "body_bytes": 90,
            "captured_bytes": 90,
            "truncated": False,
            "complete": True,
        },
        "routing": {
            "performed": True,
            "requested_model": "research",
            "configuration_id": "configuration-1",
            "session_id": None,
            "selected": {"provider": "openai", "model": "gpt-4o-mini", "effort": "low"},
            "attempts": [
                {"provider": "ollama", "model": "first-choice", "success": False},
                {"provider": "openai", "model": "gpt-4o-mini", "success": True},
            ],
        },
    }


def read_rows(db_path):
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM internal_api_audit ORDER BY request_id")]


def test_audit_preserves_complete_unicode_record_and_actual_route_after_restart(tmp_path):
    db_path = tmp_path / "memory.db"
    record = audit_record()
    expected = deepcopy(record)
    AuditStore(db_path).write(record)
    restarted = AuditStore(db_path)
    restarted.write(audit_record("request-2"))

    rows = read_rows(db_path)
    assert len(rows) == 2
    assert json.loads(rows[0]["record_json"]) == expected
    assert "日本語" in rows[0]["record_json"]
    assert rows[0]["provider"] == "openai"
    assert rows[0]["model"] == "gpt-4o-mini"
    assert rows[0]["timestamp"] == record["timestamp"]
    assert rows[0]["status_code"] == 200
    assert rows[0]["method"] == "POST"
    assert rows[0]["path"] == "/api/chat/completions"
    assert record == expected


def test_duplicate_request_ids_leave_the_first_record_intact(tmp_path):
    db_path = tmp_path / "memory.db"
    first = audit_record()
    duplicate = audit_record()
    duplicate["body"] = "This replacement must never be stored."
    duplicate["status_code"] = 500
    store = AuditStore(db_path)
    store.write(first)
    store.write(duplicate)

    rows = read_rows(db_path)
    assert len(rows) == 1
    assert json.loads(rows[0]["record_json"]) == first


def test_concurrent_writers_share_the_database_without_lost_or_duplicate_records(tmp_path):
    db_path = tmp_path / "memory.db"
    shared = AuditStore(db_path, max_entries=100)

    def write_record(index):
        # Cover shared instances and separate worker-local instances, including
        # concurrent attempts to create the lazy schema for the first time.
        store = shared if index % 2 else AuditStore(db_path, max_entries=100)
        store.write(audit_record(f"request-{index % 40:02d}"))

    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(write_record, range(80)))

    rows = read_rows(db_path)
    assert len(rows) == 40
    assert {row["request_id"] for row in rows} == {f"request-{index:02d}" for index in range(40)}
    assert all(json.loads(row["record_json"])["request_id"] == row["request_id"] for row in rows)


def test_audit_does_not_create_or_mutate_chat_configuration_and_memory_data(tmp_path):
    db_path = tmp_path / "memory.db"
    app_tables = ("sessions", "messages", "configurations", "configuration_history", "archived_memory")
    with closing(sqlite3.connect(db_path)) as connection:
        with connection:
            for table in app_tables:
                connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, content TEXT)")
                connection.execute(f"INSERT INTO {table} (content) VALUES (?)", (f"Existing {table}",))

    AuditStore(db_path).write(audit_record())

    with closing(sqlite3.connect(db_path)) as connection:
        for table in app_tables:
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == [(1, f"Existing {table}")]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == set(app_tables) | {"internal_api_audit"}


def test_retention_keeps_only_the_newest_entries(tmp_path):
    db_path = tmp_path / "memory.db"
    store = AuditStore(db_path, max_entries=3)
    for index in range(6):
        record = audit_record(f"request-{index}")
        record["timestamp"] = f"2026-09-20T14:00:0{index}+00:00"
        store.write(record)

    assert [row["request_id"] for row in read_rows(db_path)] == [
        "request-3", "request-4", "request-5",
    ]


def test_retention_removes_oldest_entries_until_payload_fits(tmp_path):
    db_path = tmp_path / "memory.db"
    sample = audit_record("sample")
    sample["body"] = "x" * 2_000
    encoded_bytes = len(json.dumps(sample, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    store = AuditStore(db_path, max_bytes=encoded_bytes * 2 + 10, max_entries=10)
    for index in range(4):
        record = audit_record(f"request-{index}")
        record["timestamp"] = f"2026-09-20T14:00:0{index}+00:00"
        record["body"] = "x" * 2_000
        store.write(record)

    rows = read_rows(db_path)
    assert [row["request_id"] for row in rows] == ["request-2", "request-3"]
    assert sum(len(row["record_json"].encode("utf-8")) for row in rows) <= store.max_bytes


def test_retention_keeps_one_oversized_newest_entry(tmp_path):
    db_path = tmp_path / "memory.db"
    store = AuditStore(db_path, max_bytes=1_000, max_entries=5)
    for index in range(2):
        record = audit_record(f"request-{index}")
        record["timestamp"] = f"2026-09-20T14:00:0{index}+00:00"
        record["body"] = "x" * 20_000
        store.write(record)

    assert [row["request_id"] for row in read_rows(db_path)] == ["request-1"]


def test_large_legacy_prune_compacts_the_database_file(tmp_path):
    db_path = tmp_path / "memory.db"
    legacy = audit_record("legacy")
    legacy["timestamp"] = "2026-09-20T14:00:00+00:00"
    legacy["body"] = "x" * 2_000_000
    AuditStore(db_path).write(legacy)
    size_before = db_path.stat().st_size

    current = audit_record("current")
    current["timestamp"] = "2026-09-20T14:00:01+00:00"
    AuditStore(db_path, max_bytes=50_000).write(current)

    assert [row["request_id"] for row in read_rows(db_path)] == ["current"]
    assert db_path.stat().st_size < size_before / 2


def test_schema_creation_is_lazy_and_follows_the_current_database_path(tmp_path):
    first_path = tmp_path / "first" / "memory.db"
    store = AuditStore(first_path)
    assert not first_path.parent.exists()
    store.write(audit_record())
    store.db_path = tmp_path / "second" / "memory.db"
    store.write(audit_record("request-2"))
    assert [row["request_id"] for row in read_rows(first_path)] == ["request-1"]
    assert [row["request_id"] for row in read_rows(store.db_path)] == ["request-2"]


def test_disabled_audit_does_not_create_files_or_serialize_records(tmp_path):
    db_path = tmp_path / "disabled" / "memory.db"
    store = AuditStore(db_path, enabled=False)
    store.write({"not_json": object()})
    assert not db_path.parent.exists()


@pytest.mark.parametrize("routing", [None, {}, {"performed": False, "selected": None}])
def test_non_inference_records_keep_null_route_columns(tmp_path, routing):
    db_path = tmp_path / "memory.db"
    record = audit_record()
    record["routing"] = routing
    record["status_code"] = None
    AuditStore(db_path).write(record)
    row = read_rows(db_path)[0]
    assert row["provider"] is None
    assert row["model"] is None
    assert row["status_code"] is None
    assert json.loads(row["record_json"]) == record


def test_values_are_parameterized_and_cannot_modify_schema(tmp_path):
    db_path = tmp_path / "memory.db"
    record = audit_record("'; DROP TABLE internal_api_audit; --")
    record["path"] = "/api/chat/'?;--"
    record["routing"]["selected"]["model"] = "'); DELETE FROM sessions; --"
    AuditStore(db_path).write(record)
    assert json.loads(read_rows(db_path)[0]["record_json"]) == record


def test_failure_reporting_is_once_and_does_not_expose_exception_data(tmp_path, caplog):
    store = AuditStore(tmp_path / "private-filename.db")
    with caplog.at_level(logging.WARNING, logger="backend.app.audit_store"):
        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(lambda _: store.report_failure(RuntimeError("SECRET BODY AND PRIVATE PATH")), range(20)))
    assert len(caplog.records) == 1
    assert "RuntimeError" in caplog.text
    assert "HTTP responses are unaffected" in caplog.text
    assert "SECRET" not in caplog.text
    assert "private-filename" not in caplog.text
    assert caplog.records[0].exc_info is None


def test_store_exposes_no_public_read_or_list_interface(tmp_path):
    store = AuditStore(tmp_path / "memory.db")
    public_methods = {name for name in dir(store) if not name.startswith("_") and callable(getattr(store, name))}
    assert public_methods == {"write", "report_failure"}


def test_connections_close_and_database_failure_is_reportable(tmp_path):
    db_path = tmp_path / "memory.db"
    store = AuditStore(db_path)
    store.write(audit_record())
    # An exclusive connection can acquire the database after writes; replacing
    # the file also catches leaked open file handles on Windows.
    moved_path = tmp_path / "moved.db"
    db_path.replace(moved_path)
    assert len(read_rows(moved_path)) == 1
    invalid_path = tmp_path / "not-a-directory"
    invalid_path.write_text("file", encoding="utf-8")
    store.db_path = invalid_path / "memory.db"
    with pytest.raises(OSError):
        store.write(audit_record("request-2"))
