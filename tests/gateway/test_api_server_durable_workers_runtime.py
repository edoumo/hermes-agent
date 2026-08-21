"""Regression tests for the H2.1 Durable Workers API runtime bridge."""
from __future__ import annotations

import threading
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import gateway.platforms.api_server_durable_runtime as runtime_module
from gateway.platforms.api_server_durable_runtime import (
    DurableWorkersRuntimeAPIServerAdapter,
)


class _Store:
    def __init__(self):
        self.finishes = []

    def finish_activation(self, *args, **kwargs):
        self.finishes.append((args, kwargs))
        return None


def _reservation():
    return {
        "status": "RESERVED",
        "worker_id": "dw-1",
        "activation_id": "activation-1",
        "message": {
            "worker_id": "dw-1",
            "message_id": "message-1",
            "state": "PROCESSING",
            "content": "continue",
        },
    }


def _build_adapter(monkeypatch, create_agent):
    adapter = object.__new__(DurableWorkersRuntimeAPIServerAdapter)
    adapter._dw_active_lock = threading.Lock()
    adapter._dw_active_lifecycles = {}
    adapter._profile_scope = lambda _profile: nullcontext()
    adapter._dw_runtime_request = lambda _session: (
        {
            "requested": {
                "model": "model-x",
                "provider": "provider-x",
            },
            "model_options": {"reasoning_effort": "high"},
            "require_model_lock": True,
        },
        {"model": "model-x", "provider": "provider-x"},
        None,
    )
    store = _Store()
    adapter._durable_worker_store = lambda: store
    adapter._create_agent = create_agent

    monkeypatch.setattr(
        "agent.subagent_lifecycle.SubagentLifecycleService",
        lambda parent_resolver: SimpleNamespace(parent_resolver=parent_resolver),
    )
    monkeypatch.setattr(
        runtime_module,
        "execute_reserved_activation",
        lambda store, lifecycle, parent_resolver, worker_id, reserved, on_started=None: {
            "status": "SUCCEEDED",
            "worker_id": worker_id,
            "activation_id": reserved["activation_id"],
        },
    )
    return adapter, store


def test_runtime_bridge_uses_native_create_agent_keyword_contract(monkeypatch):
    captured = {}

    def _create_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(session_id="session-1")

    adapter, store = _build_adapter(monkeypatch, _create_agent)
    result = adapter._dw_execute_reserved_sync(
        request_profile=None,
        session_id="session-1",
        worker_id="dw-1",
        reserved=_reservation(),
        session={"id": "session-1"},
    )

    assert result["status"] == "SUCCEEDED"
    assert store.finishes == []
    assert captured == {
        "session_id": "session-1",
        "requested_model": "model-x",
        "requested_provider": "provider-x",
        "model_options": {"reasoning_effort": "high"},
        "route": {"model": "model-x", "provider": "provider-x"},
        "session_model": None,
        "confirmed_runtime_lock": True,
    }
    assert "requested_runtime" not in captured
    assert "route_source" not in captured


def test_prelaunch_parent_failure_restores_reserved_message(monkeypatch):
    def _create_agent(**_kwargs):
        raise TypeError("parent construction failed")

    adapter, store = _build_adapter(monkeypatch, _create_agent)

    with pytest.raises(TypeError, match="parent construction failed"):
        adapter._dw_execute_reserved_sync(
            request_profile=None,
            session_id="session-1",
            worker_id="dw-1",
            reserved=_reservation(),
            session={"id": "session-1"},
        )

    assert len(store.finishes) == 1
    args, kwargs = store.finishes[0]
    assert args[:4] == (
        "session-1",
        "dw-1",
        "activation-1",
        "message-1",
    )
    assert kwargs["state"] == "FAILED_TO_START"
    assert kwargs["message_state"] == "PENDING"
    assert kwargs["worker_state"] == "DORMANT"
