"""Regression tests for H4 control-route query-string rejection."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.durable_workers_api import NotFoundError
from gateway.platforms.api_server_durable_control import (
    DurableWorkersControlAPIServerAdapter,
)


async def _session_ok(_request):
    return "session-1", {"id": "session-1"}, None


def _assert_invalid_query(response) -> None:
    assert response.status == 400
    payload = json.loads(response.text)
    assert payload["error"]["code"] == "invalid_durable_worker_request"
    assert "query" in payload["error"]["message"].lower()


def _assert_not_found(response) -> None:
    assert response.status == 404
    payload = json.loads(response.text)
    assert payload["error"]["code"] == "durable_worker_not_found"


def test_h4_query_guard_accepts_only_empty_query_string():
    empty = SimpleNamespace(query_string="", query={})
    assert DurableWorkersControlAPIServerAdapter._dw_control_query_error(empty) is None

    response = DurableWorkersControlAPIServerAdapter._dw_control_query_error(
        SimpleNamespace(query_string="limit=1")
    )
    _assert_invalid_query(response)

    response = DurableWorkersControlAPIServerAdapter._dw_control_query_error(
        SimpleNamespace(query={"x": "1"})
    )
    _assert_invalid_query(response)


@pytest.mark.asyncio
async def test_operations_rejects_query_before_reading_control_state():
    adapter = object.__new__(DurableWorkersControlAPIServerAdapter)
    adapter._dw_session_or_error = _session_ok
    adapter._dw_max_concurrent_activations = 4

    def _unexpected_control():
        raise AssertionError("operations query must be rejected before store access")

    adapter._durable_worker_control = _unexpected_control
    response = await adapter._handle_dw_operations(
        SimpleNamespace(query_string="limit=1")
    )
    _assert_invalid_query(response)


@pytest.mark.asyncio
async def test_retry_checks_read_only_worker_ownership_then_rejects_query():
    adapter = object.__new__(DurableWorkersControlAPIServerAdapter)
    adapter._dw_session_or_error = _session_ok
    sequence = []

    class Projection:
        def get_worker(self, parent, worker_id):
            sequence.append(("projection", parent, worker_id))
            return {"worker_id": worker_id, "parent_session_id": parent}

    async def _unexpected_body(_request):
        raise AssertionError("retry query must be rejected before body read")

    adapter._durable_worker_projection = lambda: Projection()
    adapter._durable_worker_store = lambda: (_ for _ in ()).throw(
        AssertionError("invalid query must not construct the mutable store")
    )
    adapter._read_json_body = _unexpected_body
    adapter._durable_worker_control = lambda: (_ for _ in ()).throw(
        AssertionError("retry query must be rejected before mutation")
    )
    request = SimpleNamespace(
        query_string="x=1",
        match_info={"worker_id": "dw-1"},
    )
    response = await adapter._handle_dw_retry_worker(request)
    _assert_invalid_query(response)
    assert sequence == [("projection", "session-1", "dw-1")]


@pytest.mark.asyncio
async def test_retry_foreign_worker_stays_404_even_with_query():
    adapter = object.__new__(DurableWorkersControlAPIServerAdapter)
    adapter._dw_session_or_error = _session_ok

    class Projection:
        def get_worker(self, _parent, _worker_id):
            raise NotFoundError("foreign worker")

    adapter._durable_worker_projection = lambda: Projection()
    adapter._durable_worker_store = lambda: (_ for _ in ()).throw(
        AssertionError("foreign query must not construct the mutable store")
    )
    adapter._read_json_body = lambda _request: (_ for _ in ()).throw(
        AssertionError("foreign worker must fail before body read")
    )
    request = SimpleNamespace(
        query_string="x=1",
        match_info={"worker_id": "dw-foreign"},
    )
    response = await adapter._handle_dw_retry_worker(request)
    _assert_not_found(response)


@pytest.mark.asyncio
async def test_cancel_checks_read_only_worker_ownership_then_rejects_query():
    adapter = object.__new__(DurableWorkersControlAPIServerAdapter)
    adapter._dw_session_or_error = _session_ok
    sequence = []

    class Projection:
        def get_worker(self, parent, worker_id):
            sequence.append(("projection", parent, worker_id))
            return {"worker_id": worker_id, "parent_session_id": parent}

    async def _unexpected_body(_request):
        raise AssertionError("cancel query must be rejected before body read")

    adapter._durable_worker_projection = lambda: Projection()
    adapter._durable_worker_store = lambda: (_ for _ in ()).throw(
        AssertionError("invalid query must not construct the mutable store")
    )
    adapter._durable_worker_control = lambda: (_ for _ in ()).throw(
        AssertionError("cancel query must be rejected before control/lifecycle lookup")
    )
    adapter._read_json_body = _unexpected_body
    request = SimpleNamespace(
        query_string="debug=1",
        match_info={"worker_id": "dw-1", "activation_id": "dwa-1"},
    )
    response = await adapter._handle_dw_cancel_activation(request)
    _assert_invalid_query(response)
    assert sequence == [("projection", "session-1", "dw-1")]


@pytest.mark.asyncio
async def test_cancel_foreign_worker_stays_404_even_with_query():
    adapter = object.__new__(DurableWorkersControlAPIServerAdapter)
    adapter._dw_session_or_error = _session_ok

    class Projection:
        def get_worker(self, _parent, _worker_id):
            raise NotFoundError("foreign worker")

    adapter._durable_worker_projection = lambda: Projection()
    adapter._durable_worker_store = lambda: (_ for _ in ()).throw(
        AssertionError("foreign query must not construct the mutable store")
    )
    adapter._durable_worker_control = lambda: (_ for _ in ()).throw(
        AssertionError("foreign worker must fail before control lookup")
    )
    adapter._read_json_body = lambda _request: (_ for _ in ()).throw(
        AssertionError("foreign worker must fail before body read")
    )
    request = SimpleNamespace(
        query_string="debug=1",
        match_info={"worker_id": "dw-foreign", "activation_id": "dwa-foreign"},
    )
    response = await adapter._handle_dw_cancel_activation(request)
    _assert_not_found(response)
