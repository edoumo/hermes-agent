"""H6 final API adapter contract tests."""
from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from agent.durable_worker_schema import (
    DURABLE_SCHEMA_VERSION,
    DurableWorkerSchemaError,
)
from agent.versioned_durable_workers import VersionedDurableWorkerStore
from gateway.platforms.api_server_durable_final import (
    DurableWorkersFinalAPIServerAdapter,
)
from gateway.platforms.api_server_durable_task_recovery import (
    DurableWorkersTaskRecoveryAPIServerAdapter,
)


def test_final_adapter_adds_no_routes_or_listener():
    adapter = object.__new__(DurableWorkersFinalAPIServerAdapter)
    inherited = object.__new__(DurableWorkersTaskRecoveryAPIServerAdapter)

    final_routes = [
        (method, path)
        for method, path, _handler in adapter._http_route_table()
    ]
    inherited_routes = [
        (method, path)
        for method, path, _handler in inherited._http_route_table()
    ]

    assert final_routes == inherited_routes
    assert len(final_routes) == len(set(final_routes))
    assert "connect" not in DurableWorkersFinalAPIServerAdapter.__dict__
    assert "_check_auth" not in DurableWorkersFinalAPIServerAdapter.__dict__


def test_final_adapter_uses_versioned_store(monkeypatch, tmp_path):
    path = tmp_path / "durable-workers.db"
    adapter = object.__new__(DurableWorkersFinalAPIServerAdapter)
    monkeypatch.setattr(adapter, "_durable_worker_db_path", lambda: path)

    store = adapter._durable_worker_store()

    assert isinstance(store, VersionedDurableWorkerStore)
    assert store.db_path == Path(path)


def test_final_adapter_preflights_storage_during_construction(monkeypatch, tmp_path):
    path = tmp_path / "durable-workers.db"
    monkeypatch.setattr(
        DurableWorkersTaskRecoveryAPIServerAdapter,
        "__init__",
        lambda self, config: None,
    )
    monkeypatch.setattr(
        DurableWorkersFinalAPIServerAdapter,
        "_durable_worker_db_path",
        lambda self: path,
    )

    adapter = DurableWorkersFinalAPIServerAdapter(object())

    assert adapter._dw_storage_schema_version == DURABLE_SCHEMA_VERSION
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_final_adapter_rejects_future_storage_before_bootstrap(monkeypatch, tmp_path):
    path = tmp_path / "durable-workers.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE future_only(marker TEXT)")
        db.execute(f"PRAGMA user_version={DURABLE_SCHEMA_VERSION + 1}")

    monkeypatch.setattr(
        DurableWorkersTaskRecoveryAPIServerAdapter,
        "__init__",
        lambda self, config: None,
    )
    monkeypatch.setattr(
        DurableWorkersFinalAPIServerAdapter,
        "_durable_worker_db_path",
        lambda self: path,
    )

    with pytest.raises(DurableWorkerSchemaError, match="newer than this Hermes build"):
        DurableWorkersFinalAPIServerAdapter(object())

    with sqlite3.connect(path) as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert tables == {"future_only"}


def test_final_adapter_remains_h5_recovery_subclass():
    assert issubclass(
        DurableWorkersFinalAPIServerAdapter,
        DurableWorkersTaskRecoveryAPIServerAdapter,
    )
