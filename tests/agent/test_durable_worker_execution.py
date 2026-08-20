"""H2 execution-seam tests for pre-reserved Durable Worker activations."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace

import pytest

from agent.durable_worker_execution import execute_reserved_activation
from agent.durable_workers import DurableWorkerConflictError


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
        self.cancel_requested = []

    def get_worker(self, parent, worker_id):
        assert parent == "session-1"
        assert worker_id == "dw-1"
        return {"role": "leaf", "model": None, "toolsets": []}

    def render_context(self, parent, worker_id, *, exclude_message_id=None):
        assert parent == "session-1"
        assert worker_id == "dw-1"
        assert exclude_message_id == "message-1"
        return "prior durable context"

    def bind_activation(self, activation_id, subagent_id):
        self.bound.append((activation_id, subagent_id))

    def finish_activation(self, *args, **kwargs):
        self.finishes.append((args, kwargs))
        return {"message_id": "report-1"}

    def mark_cancel_requested(self, *args):
        self.cancel_requested.append(args)


class _Lifecycle:
    def __init__(self, *, launch_error: Exception | None = None, completed: bool = True):
        self.launch_error = launch_error
        self.completed = completed
        self.requests = []
        self.cancelled = []

    def launch(self, request):
        self.requests.append(request)
        if self.launch_error is not None:
            raise self.launch_error
        return SimpleNamespace(subagent_id="sa-1")

    def wait(self, _handle):
        return SimpleNamespace(
            completed=self.completed,
            state=SimpleNamespace(value="SUCCEEDED" if self.completed else "RUNNING"),
        )

    def result(self, _handle):
        return SimpleNamespace(ready=True, summary="done", error_message=None)

    def cancel(self, handle, *, reason):
        self.cancelled.append((handle.subagent_id, reason))
        return SimpleNamespace(accepted=True)


def _reservation():
    return {
        "status": "RESERVED",
        "worker_id": "dw-1",
        "activation_id": "activation-1",
        "message": {
            "worker_id": "dw-1",
            "message_id": "message-1",
            "state": "PROCESSING",
            "content": "continue the durable task",
        },
    }


def _parent():
    return SimpleNamespace(session_id="session-1")


def test_executes_reserved_activation_without_launch_timeout():
    store = _Store()
    lifecycle = _Lifecycle()

    result = execute_reserved_activation(
        store, lifecycle, _parent, "dw-1", _reservation()
    )

    assert result["status"] == "SUCCEEDED"
    assert result["activation_id"] == "activation-1"
    assert result["report_message_id"] == "report-1"
    assert lifecycle.requests[0].timeout_seconds is None
    assert lifecycle.requests[0].correlation_id == "activation-1"
    assert store.bound == [("activation-1", "sa-1")]
    assert store.finishes[-1][1]["message_state"] == "CONSUMED"
    assert store.finishes[-1][1]["worker_state"] == "DORMANT"


def test_launch_failure_requeues_reserved_message():
    store = _Store()
    lifecycle = _Lifecycle(launch_error=RuntimeError("launch failed"))

    with pytest.raises(RuntimeError, match="launch failed"):
        execute_reserved_activation(
            store, lifecycle, _parent, "dw-1", _reservation()
        )

    assert store.finishes[-1][1]["state"] == "FAILED_TO_START"
    assert store.finishes[-1][1]["message_state"] == "PENDING"
    assert store.finishes[-1][1]["worker_state"] == "DORMANT"


def test_incomplete_terminal_state_fails_closed():
    store = _Store()
    lifecycle = _Lifecycle(completed=False)

    result = execute_reserved_activation(
        store, lifecycle, _parent, "dw-1", _reservation()
    )

    assert result["status"] == "CANCEL_REQUESTED"
    assert lifecycle.cancelled
    assert store.cancel_requested == [("session-1", "dw-1", "activation-1")]


def test_reservation_cannot_be_rebound_to_another_worker():
    with pytest.raises(DurableWorkerConflictError, match="worker mismatch"):
        execute_reserved_activation(
            _Store(), _Lifecycle(), _parent, "dw-other", _reservation()
        )
