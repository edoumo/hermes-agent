"""H6 schema-versioning and rollback-compatibility tests."""
from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from agent.durable_worker_schema import (
    DURABLE_SCHEMA_VERSION,
    DurableWorkerSchemaError,
    audit_schema,
)
from agent.durable_workers import DurableWorkerStore
from agent.versioned_durable_workers import VersionedDurableWorkerStore


def _user_version(path: Path) -> int:
    with sqlite3.connect(path) as db:
        return int(db.execute("PRAGMA user_version").fetchone()[0])


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as db:
        return {
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }


def test_new_versioned_store_creates_formal_v1_layout(tmp_path):
    path = tmp_path / "durable-workers.db"

    store = VersionedDurableWorkerStore(path)
    worker = store.create_worker("session-1", label="worker-1")

    assert worker["worker_id"].startswith("dw_")
    assert _user_version(path) == DURABLE_SCHEMA_VERSION == 1
    assert "durable_worker_task_runs" in _tables(path)

    audit = audit_schema(path)
    assert audit.ok is True
    assert audit.version == 1
    assert audit.quick_check == "ok"
    assert audit.foreign_key_violations == ()


def test_legacy_unversioned_h1_h5_database_is_adopted_without_data_loss(tmp_path):
    path = tmp_path / "durable-workers.db"
    legacy = DurableWorkerStore(path)
    worker = legacy.create_worker("session-1", label="legacy-worker")
    message = legacy.enqueue_message("session-1", worker["worker_id"], "legacy-message")
    task = legacy.create_task(
        "session-1",
        subject="legacy-task",
        worker_id=worker["worker_id"],
    )

    assert _user_version(path) == 0
    assert "durable_worker_task_runs" not in _tables(path)

    adopted = VersionedDurableWorkerStore(path)

    assert _user_version(path) == 1
    assert adopted.get_worker("session-1", worker["worker_id"])["label"] == "legacy-worker"
    assert adopted.list_messages("session-1", worker["worker_id"])[0]["message_id"] == message["message_id"]
    assert adopted.get_task("session-1", task["task_id"])["subject"] == "legacy-task"
    assert "durable_worker_task_runs" in _tables(path)
    assert audit_schema(path).ok is True


def test_future_schema_is_rejected_before_h1_bootstrap_mutates_database(tmp_path):
    path = tmp_path / "durable-workers.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE future_only(marker TEXT NOT NULL)")
        db.execute("INSERT INTO future_only VALUES('keep-me')")
        db.execute(f"PRAGMA user_version={DURABLE_SCHEMA_VERSION + 1}")

    before_tables = _tables(path)

    with pytest.raises(DurableWorkerSchemaError, match="newer than this Hermes build"):
        VersionedDurableWorkerStore(path)

    assert _tables(path) == before_tables == {"future_only"}
    assert _user_version(path) == DURABLE_SCHEMA_VERSION + 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT marker FROM future_only").fetchone()[0] == "keep-me"


def test_audit_detects_foreign_key_violation_without_repairing_it(tmp_path):
    path = tmp_path / "durable-workers.db"
    VersionedDurableWorkerStore(path)

    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "INSERT INTO durable_worker_messages "
            "(message_id,worker_id,direction,content,state,created_at,updated_at) "
            "VALUES('bad-message','missing-worker','parent','x','PENDING',1,1)"
        )

    audit = audit_schema(path)
    assert audit.ok is False
    assert audit.quick_check == "ok"
    assert audit.foreign_key_violations

    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM durable_worker_messages WHERE message_id='bad-message'"
        ).fetchone()[0] == 1


def test_h5_store_can_reopen_h6_v1_database_for_rollback(tmp_path):
    path = tmp_path / "durable-workers.db"
    h6 = VersionedDurableWorkerStore(path)
    worker = h6.create_worker("session-1", label="rollback-worker")
    h6.enqueue_message("session-1", worker["worker_id"], "rollback-message")

    # H5 code ignores PRAGMA user_version. Because H6 v1 is deliberately the
    # H5 layout, reverting the executable does not require a database downgrade.
    h5 = DurableWorkerStore(path)

    assert h5.get_worker("session-1", worker["worker_id"])["label"] == "rollback-worker"
    assert h5.list_messages("session-1", worker["worker_id"])[0]["content"] == "rollback-message"
    assert _user_version(path) == DURABLE_SCHEMA_VERSION
