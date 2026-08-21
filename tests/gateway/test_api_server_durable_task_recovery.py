"""H5 task recovery API tests."""
from __future__ import annotations

import asyncio
import json

import pytest

import gateway.platforms.api_server_durable_task_recovery as recovery_module
from gateway.platforms.api_server_durable_task_recovery import (
    DurableWorkersTaskRecoveryAPIServerAdapter,
)


async def _session_ok(_request):
    return "session-1", {"id": "session-1"}, None


async def _owned(_session_id, _task_id):
    return {"task_id": "task-1", "revision": 7}, None


def test_h5_recovery_route_extends_orchestration_without_new_listener():
    adapter = object.__new__(DurableWorkersTaskRecoveryAPIServerAdapter)
    routes = {(method, path) for method, path, _handler in adapter._http_route_table()}

    assert ("GET", "/api/sessions/{session_id}/worker-task-graph") in routes
    assert (
        "POST",
        "/api/sessions/{session_id}/worker-tasks/{task_id}/recover",
    ) in routes
    assert "connect" not in DurableWorkersTaskRecoveryAPIServerAdapter.__dict__


@pytest.mark.asyncio
async def test_recover_handler_returns_recovery_ready(monkeypatch):
    adapter = object.__new__(DurableWorkersTaskRecoveryAPIServerAdapter)
    adapter._dw_session_or_error = _session_ok
    adapter._dw_h5_owned_task_or_error = _owned
    adapter._durable_worker_store = lambda: object()

    async def _body(_request):
        return {"expected_revision": 7}, None

    adapter._read_json_body = _body

    class FakeOrchestrator:
        def __init__(self, _store):
            pass

        def recover_failed_task(self, parent, task_id, *, expected_revision):
            assert (parent, task_id, expected_revision) == ("session-1", "task-1", 7)
            return {
                "status": "RECOVERY_READY",
                "task": {"task_id": task_id, "status": "pending", "revision": 8},
                "worker": {"worker_id": "dw-1", "status": "DORMANT"},
                "message_id": "dwm-1",
                "previous_activation_id": "dwa-old",
            }

    monkeypatch.setattr(
        recovery_module, "RecoverableDurableTaskOrchestrator", FakeOrchestrator
    )
    request = type(
        "Request",
        (),
        {"query_string": "", "query": {}, "match_info": {"task_id": "task-1"}},
    )()

    response = await adapter._handle_dw_task_recover(request)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["status"] == "RECOVERY_READY"
    assert payload["task"]["status"] == "pending"
    assert payload["worker"]["status"] == "DORMANT"


@pytest.mark.asyncio
async def test_recovery_dispatch_reports_reused_message(monkeypatch):
    adapter = object.__new__(DurableWorkersTaskRecoveryAPIServerAdapter)
    adapter._dw_session_or_error = _session_ok
    adapter._dw_h5_owned_task_or_error = _owned
    adapter._dw_runtime_request = lambda _session: ({}, None, None)
    adapter._runtime_lock_error = lambda _runtime: None
    adapter._dw_dispatch_lock = None
    adapter._dw_activation_tasks = set()
    adapter._dw_max_concurrent_activations = 4
    adapter._durable_worker_store = lambda: object()

    async def _body(_request):
        return {"expected_revision": 7}, None

    adapter._read_json_body = _body

    class FakeOrchestrator:
        def __init__(self, _store):
            pass

        def reserve_ready_task(self, parent, task_id, *, expected_revision):
            return {
                "status": "RESERVED",
                "task_id": task_id,
                "worker_id": "dw-1",
                "activation_id": "dwa-new",
                "message": {"message_id": "dwm-old"},
                "reused_message": True,
            }

    monkeypatch.setattr(
        recovery_module, "RecoverableDurableTaskOrchestrator", FakeOrchestrator
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def _background(**_kwargs):
        started.set()
        await release.wait()

    adapter._dw_execute_reserved_background = _background
    request = type(
        "Request",
        (),
        {"query_string": "", "query": {}, "match_info": {"task_id": "task-1"}},
    )()

    response = await adapter._handle_dw_task_dispatch(request)
    payload = json.loads(response.text)
    await asyncio.wait_for(started.wait(), timeout=1)

    assert response.status == 202
    assert payload["message_id"] == "dwm-old"
    assert payload["activation_id"] == "dwa-new"
    assert payload["reused_message"] is True

    task = next(iter(adapter._dw_activation_tasks))
    release.set()
    await task
