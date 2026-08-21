"""H4 execution policy tests for completed operator cancellation."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace

import pytest

import agent.durable_worker_execution as execution


@dataclass
class _Request:
    goal: str
    context: str | None = None
    role: str = "leaf"
    model: str | None = None
    allowed_toolsets: tuple[str, ...] | None = None
    blocked_tools: tuple[str, ...] = ()
    working_directory: str | None = None
    parent_session_id: str | None = None
    correlation_id: str | None = None
    metadata: dict | None = None
    timeout_seconds: float | None = None


@pytest.fixture(autouse=True)
def _stub_lifecycle_contract(monkeypatch):
    module = ModuleType("agent.subagent_lifecycle")
    module.SubagentLaunchRequest = _Request
    monkeypatch.setitem(sys.modules, "agent.subagent_lifecycle", module)


class _Store:
    def __init__(self):
        self.finishes = []
        self.bound = []

    def get_worker(self, parent, worker_id):
        assert parent == "session-1"
        assert worker_id == "dw-1"
        return {"role": "leaf", "model": None, "toolsets": []}

    def render_context(self, parent, worker_id, *, exclude_message_id=None):
        return "durable context"

    def bind_activation(self, activation_id, subagent_id):
        self.bound.append((activation_id, subagent_id))

    def finish_activation(self, *args, **kwargs):
        self.finishes.append((args, kwargs))
        return None

    def mark_cancel_requested(self, *args):
        raise AssertionError("terminal cancellation should not request a second cancel")


class _CancelledLifecycle:
    def launch(self, _request):
        return SimpleNamespace(subagent_id="sa-cancelled")

    def wait(self, _handle):
        return SimpleNamespace(
            completed=True,
            state=SimpleNamespace(value="CANCELLED"),
        )

    def result(self, _handle):
        return SimpleNamespace(
            ready=False,
            summary=None,
            error_message="operator interrupt acknowledged",
        )


def _reservation():
    return {
        "status": "RESERVED",
        "worker_id": "dw-1",
        "activation_id": "activation-1",
        "message": {
            "worker_id": "dw-1",
            "message_id": "message-1",
            "state": "PROCESSING",
            "content": "work to cancel",
        },
    }


def _parent():
    return SimpleNamespace(session_id="session-1")


def test_completed_operator_cancel_requeues_message_and_releases_worker(monkeypatch):
    class _Control:
        def __init__(self, store):
            assert isinstance(store, _Store)

        def is_cancel_requested(self, parent, worker_id, activation_id):
            assert (parent, worker_id, activation_id) == (
                "session-1",
                "dw-1",
                "activation-1",
            )
            return True

    monkeypatch.setattr(execution, "DurableWorkerControl", _Control)
    store = _Store()

    result = execution.execute_reserved_activation(
        store,
        _CancelledLifecycle(),
        _parent,
        "dw-1",
        _reservation(),
    )

    assert result == {
        "worker_id": "dw-1",
        "activation_id": "activation-1",
        "subagent_id": "sa-cancelled",
        "status": "CANCELLED",
        "retryable": True,
    }
    assert store.finishes[-1][1]["state"] == "CANCELLED"
    assert store.finishes[-1][1]["message_state"] == "PENDING"
    assert store.finishes[-1][1]["worker_state"] == "DORMANT"


def test_unmarked_cancelled_child_keeps_fail_closed_worker_state(monkeypatch):
    class _Control:
        def __init__(self, _store):
            pass

        def is_cancel_requested(self, _parent, _worker_id, _activation_id):
            return False

    monkeypatch.setattr(execution, "DurableWorkerControl", _Control)
    store = _Store()

    result = execution.execute_reserved_activation(
        store,
        _CancelledLifecycle(),
        _parent,
        "dw-1",
        _reservation(),
    )

    assert result["status"] == "CANCELLED"
    assert result["error"] == "operator interrupt acknowledged"
    assert store.finishes[-1][1]["message_state"] == "FAILED"
    assert store.finishes[-1][1]["worker_state"] == "FAILED"
