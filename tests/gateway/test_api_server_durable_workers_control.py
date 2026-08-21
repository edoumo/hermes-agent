"""H4 API contract tests for Durable Worker operational controls."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from gateway.platforms.api_server_durable_control import (
    DurableWorkersControlAPIServerAdapter,
)


async def _session_ok(_request):
    return "session-1", {"id": "session-1"}, None


def _allow_owned_worker(adapter, *, worker_id="dw-1"):
    """Stub the H4 read-only ownership projection for unit-handler tests."""

    class Projection:
        def get_worker(self, parent, requested_worker_id):
            assert parent == "session-1"
            assert requested_worker_id == worker_id
            return {
                "worker_id": requested_worker_id,
                "parent_session_id": parent,
                "status": "RUNNING",
            }

    adapter._durable_worker_projection = lambda: Projection()


def test_h4_control_routes_extend_h21_without_new_listener():
    adapter = object.__new__(DurableWorkersControlAPIServerAdapter)
    routes = [(method, path) for method, path, _handler in adapter._http_route_table()]

    assert ("GET", "/api/sessions/{session_id}/worker-events") in routes
    assert ("GET", "/api/sessions/{session_id}/worker-operations") in routes
    assert (
        "POST",
        "/api/sessions/{session_id}/workers/{worker_id}/retry",
    ) in routes
    assert (
        "POST",
        "/api/sessions/{session_id}/workers/{worker_id}/activations/{activation_id}/cancel",
    ) in routes
    assert len(routes) == len(set(routes))
    assert "connect" not in DurableWorkersControlAPIServerAdapter.__dict__
    assert "_check_auth" not in DurableWorkersControlAPIServerAdapter.__dict__


@pytest.mark.asyncio
async def test_operations_summary_is_session_scoped_and_reports_limit():
    adapter = object.__new__(DurableWorkersControlAPIServerAdapter)
    adapter._dw_session_or_error = _session_ok
    adapter._dw_max_concurrent_activations = 4

    class Control:
        def session_summary(self, parent):
            assert parent == "session-1"
            return {
                "workers": {"total": 2, "DORMANT": 1, "RUNNING": 1, "FAILED": 0, "DISABLED": 0},
                "activations": {"STARTING": 0, "RUNNING": 1, "CANCEL_REQUESTED": 0},
                "messages": {"PENDING": 1, "PROCESSING": 1, "FAILED": 0},
            }

    adapter._durable_worker_control = lambda: Control()
    response = await adapter._handle_dw_operations(SimpleNamespace())
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["session_id"] == "session-1"
    assert payload["configured_max_concurrent_activations"] == 4
    assert payload["workers"]["RUNNING"] == 1
    assert "global_active" not in payload
    assert "owner_pid" not in response.text


@pytest.mark.asyncio
async def test_retry_handler_passes_revision_cas_and_returns_retry_ready():
    adapter = object.__new__(DurableWorkersControlAPIServerAdapter)
    adapter._dw_session_or_error = _session_ok
    _allow_owned_worker(adapter)

    async def _body(_request):
        return {"expected_revision": 7}, None

    adapter._read_json_body = _body
    calls = []

    class Control:
        def retry_failed_worker(self, parent, worker_id, *, expected_revision=None):
            calls.append((parent, worker_id, expected_revision))
            return {
                "worker": {"worker_id": worker_id, "status": "DORMANT", "revision": 8},
                "message_id": "message-1",
                "previous_activation_id": "activation-failed",
                "status": "RETRY_READY",
            }

    adapter._durable_worker_control = lambda: Control()
    request = SimpleNamespace(match_info={"worker_id": "dw-1"})
    response = await adapter._handle_dw_retry_worker(request)
    payload = json.loads(response.text)

    assert response.status == 200
    assert calls == [("session-1", "dw-1", 7)]
    assert payload["status"] == "RETRY_READY"
    assert payload["worker"]["status"] == "DORMANT"


@pytest.mark.asyncio
async def test_cancel_requires_locally_supervised_matching_activation():
    adapter = object.__new__(DurableWorkersControlAPIServerAdapter)
    adapter._dw_session_or_error = _session_ok
    _allow_owned_worker(adapter)
    adapter._dw_active_lock = threading.Lock()
    adapter._dw_active_lifecycles = {}

    async def _body(_request):
        return {"reason": "operator requested"}, None

    adapter._read_json_body = _body
    requested = []

    class Control:
        def get_activation(self, parent, worker_id, activation_id):
            assert (parent, worker_id, activation_id) == (
                "session-1",
                "dw-1",
                "activation-1",
            )
            return {
                "activation_id": activation_id,
                "worker_id": worker_id,
                "subagent_id": "sa-1",
                "state": "RUNNING",
            }

        def request_cancel(self, *args):
            requested.append(args)
            raise AssertionError("must not persist cancellation without a live handle")

    adapter._durable_worker_control = lambda: Control()
    request = SimpleNamespace(
        match_info={"worker_id": "dw-1", "activation_id": "activation-1"}
    )
    response = await adapter._handle_dw_cancel_activation(request)
    payload = json.loads(response.text)

    assert response.status == 409
    assert payload["error"]["code"] == "durable_worker_activation_not_locally_supervised"
    assert requested == []


@pytest.mark.asyncio
async def test_cancel_marks_durable_intent_before_lifecycle_interrupt():
    adapter = object.__new__(DurableWorkersControlAPIServerAdapter)
    adapter._dw_session_or_error = _session_ok
    _allow_owned_worker(adapter)
    adapter._dw_active_lock = threading.Lock()
    sequence = []

    async def _body(_request):
        return {"reason": "stop this turn"}, None

    adapter._read_json_body = _body

    class Control:
        def get_activation(self, parent, worker_id, activation_id):
            return {
                "activation_id": activation_id,
                "worker_id": worker_id,
                "subagent_id": "sa-1",
                "state": "RUNNING",
            }

        def request_cancel(self, parent, worker_id, activation_id):
            sequence.append("durable-marker")
            return {
                "state": "CANCEL_REQUESTED",
                "changed": True,
                "previous_state": "RUNNING",
            }

        def restore_cancel_request(self, *args, **kwargs):
            sequence.append("rollback")
            return True

    class Lifecycle:
        def cancel(self, handle, *, reason):
            assert handle.subagent_id == "sa-1"
            assert reason == "stop this turn"
            sequence.append("lifecycle-cancel")
            return SimpleNamespace(
                accepted=True,
                already_terminal=False,
                unknown_handle=False,
                unsupported=False,
            )

    handle = SimpleNamespace(subagent_id="sa-1")
    adapter._dw_active_lifecycles = {
        "activation-1": (Lifecycle(), handle)
    }
    adapter._durable_worker_control = lambda: Control()
    request = SimpleNamespace(
        match_info={"worker_id": "dw-1", "activation_id": "activation-1"}
    )

    response = await adapter._handle_dw_cancel_activation(request)
    payload = json.loads(response.text)

    assert response.status == 202
    assert payload["status"] == "CANCEL_REQUESTED"
    assert payload["accepted"] is True
    assert sequence == ["durable-marker", "lifecycle-cancel"]


@pytest.mark.asyncio
async def test_cancel_rejection_rolls_back_durable_marker():
    adapter = object.__new__(DurableWorkersControlAPIServerAdapter)
    adapter._dw_session_or_error = _session_ok
    _allow_owned_worker(adapter)
    adapter._dw_active_lock = threading.Lock()

    async def _body(_request):
        return {}, None

    adapter._read_json_body = _body
    rollbacks = []

    class Control:
        def get_activation(self, _parent, worker_id, activation_id):
            return {
                "activation_id": activation_id,
                "worker_id": worker_id,
                "subagent_id": "sa-1",
                "state": "RUNNING",
            }

        def request_cancel(self, _parent, _worker_id, _activation_id):
            return {
                "state": "CANCEL_REQUESTED",
                "changed": True,
                "previous_state": "RUNNING",
            }

        def restore_cancel_request(self, *args, **kwargs):
            rollbacks.append((args, kwargs))
            return True

    class Lifecycle:
        def cancel(self, _handle, *, reason):
            return SimpleNamespace(
                accepted=False,
                already_terminal=False,
                unknown_handle=False,
                unsupported=True,
            )

    adapter._dw_active_lifecycles = {
        "activation-1": (Lifecycle(), SimpleNamespace(subagent_id="sa-1"))
    }
    adapter._durable_worker_control = lambda: Control()
    request = SimpleNamespace(
        match_info={"worker_id": "dw-1", "activation_id": "activation-1"}
    )

    response = await adapter._handle_dw_cancel_activation(request)
    payload = json.loads(response.text)

    assert response.status == 409
    assert payload["error"]["code"] == "durable_worker_cancellation_unavailable"
    assert len(rollbacks) == 1
    assert rollbacks[0][1]["previous_state"] == "RUNNING"
