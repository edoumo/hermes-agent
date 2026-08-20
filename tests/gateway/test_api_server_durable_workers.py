"""Contract tests for the experimental H2.1 Durable Workers API adapter."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.durable_workers import DurableWorkerError
from gateway.platforms.api_server_durable import DurableWorkersAPIServerAdapter


def test_durable_worker_routes_extend_native_api_table():
    adapter = object.__new__(DurableWorkersAPIServerAdapter)
    routes = {(method, path) for method, path, _handler in adapter._http_route_table()}

    # Existing Hermes session/run surfaces remain present.
    assert ("GET", "/api/sessions") in routes
    assert ("GET", "/api/sessions/{session_id}") in routes
    assert ("POST", "/v1/runs") in routes

    # H2 read contract remains intact.
    assert ("GET", "/api/sessions/{session_id}/workers") in routes
    assert ("GET", "/api/sessions/{session_id}/workers/{worker_id}") in routes
    assert (
        "GET",
        "/api/sessions/{session_id}/workers/{worker_id}/messages",
    ) in routes
    assert (
        "GET",
        "/api/sessions/{session_id}/workers/{worker_id}/activations",
    ) in routes
    assert ("GET", "/api/sessions/{session_id}/worker-tasks") in routes

    # H2.1 adds a bounded control plane and one session-scoped SSE feed.
    assert ("POST", "/api/sessions/{session_id}/workers") in routes
    assert (
        "POST",
        "/api/sessions/{session_id}/workers/{worker_id}/messages",
    ) in routes
    assert (
        "POST",
        "/api/sessions/{session_id}/workers/{worker_id}/run",
    ) in routes
    assert ("POST", "/api/sessions/{session_id}/worker-tasks") in routes
    assert (
        "POST",
        "/api/sessions/{session_id}/worker-tasks/{task_id}/status",
    ) in routes
    assert (
        "POST",
        "/api/sessions/{session_id}/worker-tasks/{task_id}/dependencies",
    ) in routes
    assert ("GET", "/api/sessions/{session_id}/worker-events") in routes

    assert not any(
        method in {"DELETE", "PUT", "PATCH"}
        and ("/workers" in path or "/worker-tasks" in path)
        for method, path in routes
    )


def test_durable_worker_route_names_are_unique():
    adapter = object.__new__(DurableWorkersAPIServerAdapter)
    routes = [(method, path) for method, path, _handler in adapter._http_route_table()]
    assert len(routes) == len(set(routes))


def test_h2_adapter_does_not_add_a_second_listener_contract():
    # The extension is a subclass of the existing API server; it has no
    # connect/listener implementation of its own. Listener/auth/CORS behavior
    # therefore remains owned by APIServerAdapter.
    assert "connect" not in DurableWorkersAPIServerAdapter.__dict__
    assert "_check_auth" not in DurableWorkersAPIServerAdapter.__dict__
    assert "_origin_allowed" not in DurableWorkersAPIServerAdapter.__dict__


def test_h2_identifier_rejects_control_characters():
    assert (
        DurableWorkersAPIServerAdapter._dw_identifier(
            "message-1", field="message_id"
        )
        == "message-1"
    )
    with pytest.raises(DurableWorkerError):
        DurableWorkersAPIServerAdapter._dw_identifier(
            "bad\nvalue", field="message_id"
        )


class _FakeStore:
    def __init__(self):
        self.messages = {}
        self.reserved = None

    def enqueue_message(self, session_id, worker_id, message, *, message_id=None):
        key = message_id or "generated"
        old = self.messages.get(key)
        if old is not None:
            if old["content"] != message:
                raise DurableWorkerError("message conflict")
            return {**old, "created": False}
        item = {
            "message_id": key,
            "worker_id": worker_id,
            "content": message,
            "state": "PENDING",
            "created": True,
        }
        self.messages[key] = item
        return item

    def reserve_next_activation(self, session_id, worker_id):
        self.reserved = {
            "status": "RESERVED",
            "worker_id": worker_id,
            "activation_id": "activation-1",
            "message": {
                "worker_id": worker_id,
                "message_id": "message-1",
                "state": "PROCESSING",
                "content": "continue",
            },
        }
        return self.reserved


async def _session_ok(_request):
    return "session-1", {"id": "session-1"}, None


@pytest.mark.asyncio
async def test_enqueue_handler_preserves_message_id_idempotency():
    adapter = object.__new__(DurableWorkersAPIServerAdapter)
    store = _FakeStore()
    adapter._dw_session_or_error = _session_ok
    adapter._durable_worker_store = lambda: store

    async def _body(_request):
        return {"message": "hello", "message_id": "stable-message"}, None

    adapter._read_json_body = _body
    request = SimpleNamespace(match_info={"worker_id": "dw-1"})

    first = await adapter._handle_dw_enqueue_message(request)
    second = await adapter._handle_dw_enqueue_message(request)
    first_payload = json.loads(first.text)
    second_payload = json.loads(second.text)

    assert first.status == 201
    assert second.status == 200
    assert first_payload["message"]["message_id"] == "stable-message"
    assert second_payload["message"]["created"] is False


@pytest.mark.asyncio
async def test_run_handler_returns_reserved_activation_before_background_completion():
    adapter = object.__new__(DurableWorkersAPIServerAdapter)
    store = _FakeStore()
    adapter._dw_session_or_error = _session_ok
    adapter._durable_worker_store = lambda: store
    adapter._dw_runtime_request = lambda _session: ({}, None, None)
    adapter._runtime_lock_error = lambda _runtime: None
    adapter._dw_dispatch_lock = None
    adapter._dw_activation_tasks = set()
    adapter._dw_max_concurrent_activations = 4
    started = asyncio.Event()
    release = asyncio.Event()

    async def _background(**_kwargs):
        started.set()
        await release.wait()

    adapter._dw_execute_reserved_background = _background
    request = SimpleNamespace(match_info={"worker_id": "dw-1"})

    response = await adapter._handle_dw_run(request)
    payload = json.loads(response.text)
    await asyncio.wait_for(started.wait(), timeout=1)

    assert response.status == 202
    assert payload["activation_id"] == "activation-1"
    assert payload["message_id"] == "message-1"
    assert payload["status"] == "STARTING"
    assert len(adapter._dw_activation_tasks) == 1

    task = next(iter(adapter._dw_activation_tasks))
    release.set()
    await task


@pytest.mark.asyncio
async def test_event_stream_rejects_invalid_last_event_id_before_prepare():
    adapter = object.__new__(DurableWorkersAPIServerAdapter)
    adapter._dw_session_or_error = _session_ok
    adapter._dw_event_subscribers = 0
    request = SimpleNamespace(headers={"Last-Event-ID": "not-valid"})

    response = await adapter._handle_dw_events(request)

    assert response.status == 400
    payload = json.loads(response.text)
    assert payload["error"]["code"] == "invalid_event_cursor"
