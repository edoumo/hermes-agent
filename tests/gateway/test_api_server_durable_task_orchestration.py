"""H5 API contract tests for Durable Task orchestration."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import gateway.platforms.api_server_durable_orchestration as orchestration_module
from gateway.platforms.api_server_durable_control import (
    DurableWorkersControlAPIServerAdapter,
)
from gateway.platforms.api_server_durable_orchestration import (
    DurableWorkersTaskOrchestrationAPIServerAdapter,
)


async def _session_ok(_request):
    return "session-1", {"id": "session-1"}, None


def test_h5_routes_extend_h4_without_new_listener_or_auth_surface():
    adapter = object.__new__(DurableWorkersTaskOrchestrationAPIServerAdapter)
    routes = [(method, path) for method, path, _handler in adapter._http_route_table()]

    assert ("POST", "/api/sessions/{session_id}/workers/{worker_id}/retry") in routes
    assert ("GET", "/api/sessions/{session_id}/worker-task-graph") in routes
    assert ("POST", "/api/sessions/{session_id}/worker-tasks/{task_id}/edit") in routes
    assert (
        "POST",
        "/api/sessions/{session_id}/worker-tasks/{task_id}/dependencies/add",
    ) in routes
    assert (
        "POST",
        "/api/sessions/{session_id}/worker-tasks/{task_id}/dependencies/remove",
    ) in routes
    assert (
        "POST",
        "/api/sessions/{session_id}/worker-tasks/{task_id}/dispatch",
    ) in routes
    assert len(routes) == len(set(routes))
    assert "connect" not in DurableWorkersTaskOrchestrationAPIServerAdapter.__dict__
    assert "_check_auth" not in DurableWorkersTaskOrchestrationAPIServerAdapter.__dict__


@pytest.mark.asyncio
async def test_graph_handler_returns_empty_projection_without_initializing_store(tmp_path):
    adapter = object.__new__(DurableWorkersTaskOrchestrationAPIServerAdapter)
    adapter._dw_session_or_error = _session_ok
    missing = tmp_path / "missing.db"
    adapter._durable_worker_db_path = lambda: missing
    request = SimpleNamespace(query={})

    response = await adapter._handle_dw_task_graph(request)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["tasks"] == []
    assert payload["edges"] == []
    assert payload["counts"]["total"] == 0
    assert missing.exists() is False


@pytest.mark.asyncio
async def test_task_edit_checks_ownership_before_query_contract():
    adapter = object.__new__(DurableWorkersTaskOrchestrationAPIServerAdapter)
    adapter._dw_session_or_error = _session_ok
    ownership_calls = []

    async def _owned(session_id, task_id):
        ownership_calls.append((session_id, task_id))
        return None, SimpleNamespace(status=404, text="not found")

    adapter._dw_h5_owned_task_or_error = _owned
    request = SimpleNamespace(
        query_string="x=1",
        query={"x": "1"},
        match_info={"task_id": "foreign-task"},
    )

    response = await adapter._handle_dw_task_edit(request)

    assert response.status == 404
    assert ownership_calls == [("session-1", "foreign-task")]


@pytest.mark.asyncio
async def test_dispatch_returns_reserved_ids_before_background_completion(monkeypatch):
    adapter = object.__new__(DurableWorkersTaskOrchestrationAPIServerAdapter)
    adapter._dw_session_or_error = _session_ok
    adapter._dw_h5_owned_task_or_error = lambda *_args: None  # replaced below

    async def _owned(_session_id, _task_id):
        return {"task_id": "task-1", "revision": 3}, None

    adapter._dw_h5_owned_task_or_error = _owned

    async def _body(_request):
        return {"expected_revision": 3}, None

    adapter._read_json_body = _body
    adapter._dw_runtime_request = lambda _session: ({}, None, None)
    adapter._runtime_lock_error = lambda _runtime: None
    adapter._dw_dispatch_lock = None
    adapter._dw_activation_tasks = set()
    adapter._dw_max_concurrent_activations = 4
    adapter._durable_worker_store = lambda: object()
    started = asyncio.Event()
    release = asyncio.Event()

    class FakeOrchestrator:
        def __init__(self, _store):
            pass

        def reserve_ready_task(self, parent, task_id, *, expected_revision):
            assert (parent, task_id, expected_revision) == ("session-1", "task-1", 3)
            return {
                "status": "RESERVED",
                "task_id": "task-1",
                "worker_id": "dw-1",
                "activation_id": "dwa-1",
                "message": {"message_id": "dwm-1"},
            }

    monkeypatch.setattr(orchestration_module, "DurableTaskOrchestrator", FakeOrchestrator)

    async def _background(**kwargs):
        assert kwargs["reserved"]["task_id"] == "task-1"
        started.set()
        await release.wait()

    adapter._dw_execute_reserved_background = _background
    request = SimpleNamespace(
        query_string="",
        query={},
        match_info={"task_id": "task-1"},
    )

    response = await adapter._handle_dw_task_dispatch(request)
    payload = json.loads(response.text)
    await asyncio.wait_for(started.wait(), timeout=1)

    assert response.status == 202
    assert payload["task_id"] == "task-1"
    assert payload["worker_id"] == "dw-1"
    assert payload["activation_id"] == "dwa-1"
    assert payload["message_id"] == "dwm-1"
    assert payload["status"] == "STARTING"

    task = next(iter(adapter._dw_activation_tasks))
    release.set()
    await task


def test_task_reserved_execution_reconciles_result_without_changing_normal_runs(monkeypatch):
    adapter = object.__new__(DurableWorkersTaskOrchestrationAPIServerAdapter)
    calls = []

    def _base_execute(_self, **kwargs):
        return {
            "worker_id": kwargs["worker_id"],
            "activation_id": kwargs["reserved"]["activation_id"],
            "status": "SUCCEEDED",
            "summary": "done",
        }

    monkeypatch.setattr(
        DurableWorkersControlAPIServerAdapter,
        "_dw_execute_reserved_sync",
        _base_execute,
    )
    adapter._durable_worker_store = lambda: object()

    class FakeOrchestrator:
        def __init__(self, _store):
            pass

        def reconcile_result(self, parent, task_id, activation_id, result):
            calls.append((parent, task_id, activation_id, result["status"]))

        def reconcile_exception(self, *args, **kwargs):
            raise AssertionError("unexpected exception reconciliation")

    monkeypatch.setattr(orchestration_module, "DurableTaskOrchestrator", FakeOrchestrator)

    normal = adapter._dw_execute_reserved_sync(
        session_id="session-1",
        worker_id="dw-1",
        reserved={"activation_id": "dwa-normal"},
    )
    assert normal["status"] == "SUCCEEDED"
    assert calls == []

    tasked = adapter._dw_execute_reserved_sync(
        session_id="session-1",
        worker_id="dw-1",
        reserved={"activation_id": "dwa-task", "task_id": "task-1"},
    )
    assert tasked["task_id"] == "task-1"
    assert calls == [("session-1", "task-1", "dwa-task", "SUCCEEDED")]
