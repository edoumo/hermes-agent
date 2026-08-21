"""H6/H6.1 final API adapter contract tests."""
from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from agent.durable_worker_preferences import ManagedVersionedDurableWorkerStore
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


_H61_ROUTES = {
    ("POST", "/api/sessions/{session_id}/workers/{worker_id}/edit"),
    ("POST", "/api/sessions/{session_id}/workers/{worker_id}/archive"),
    ("POST", "/api/sessions/{session_id}/workers/{worker_id}/restore"),
}


def test_final_adapter_adds_only_reversible_h61_routes_and_no_listener():
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

    assert set(final_routes) - set(inherited_routes) == _H61_ROUTES
    assert len(final_routes) == len(inherited_routes) + len(_H61_ROUTES)
    assert len(final_routes) == len(set(final_routes))
    assert all(method == "POST" for method, _path in _H61_ROUTES)
    assert "connect" not in DurableWorkersFinalAPIServerAdapter.__dict__
    assert "_check_auth" not in DurableWorkersFinalAPIServerAdapter.__dict__


def test_final_adapter_uses_and_caches_managed_versioned_store(monkeypatch, tmp_path):
    path = tmp_path / "durable-workers.db"
    adapter = object.__new__(DurableWorkersFinalAPIServerAdapter)
    monkeypatch.setattr(adapter, "_durable_worker_db_path", lambda: path)

    first = adapter._durable_worker_store()
    second = adapter._durable_worker_store()

    assert isinstance(first, ManagedVersionedDurableWorkerStore)
    assert isinstance(first, VersionedDurableWorkerStore)
    assert first.db_path == Path(path)
    assert second is first


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
    assert isinstance(adapter._dw_versioned_store, ManagedVersionedDurableWorkerStore)
    assert isinstance(adapter._dw_versioned_store, VersionedDurableWorkerStore)
    assert adapter._durable_worker_store() is adapter._dw_versioned_store
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
