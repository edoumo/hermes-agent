"""H6 final API adapter contract tests."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


def test_final_adapter_remains_h5_recovery_subclass():
    assert issubclass(
        DurableWorkersFinalAPIServerAdapter,
        DurableWorkersTaskRecoveryAPIServerAdapter,
    )
