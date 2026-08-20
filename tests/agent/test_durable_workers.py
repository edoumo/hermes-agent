"""H1 tests for activation-level durable worker continuity."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace

import pytest

from agent.durable_workers import (
    DurableWorkerAuthorizationError,
    DurableWorkerConflictError,
    DurableWorkerService,
    DurableWorkerStore,
)


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


class _State:
    def __init__(self, value: str):
        self.value = value


class _Lifecycle:
    def __init__(self, summaries):
        self.summaries = iter(summaries)
        self.requests = []
        self._results = {}
        self._counter = 0

    def launch(self, request):
        self.requests.append(request)
        self._counter += 1
        handle = SimpleNamespace(subagent_id=f"sa-{self._counter}")
        self._results[handle.subagent_id] = next(self.summaries)
        return handle

    def wait(self, handle, timeout_seconds=None):
        return SimpleNamespace(completed=True, state=_State("SUCCEEDED"))

    def result(self, handle):
        return SimpleNamespace(
            ready=True,
            summary=self._results[handle.subagent_id],
            error_message=None,
        )

    def cancel(self, handle, reason):
        return SimpleNamespace(accepted=True)


@pytest.fixture(autouse=True)
def _stub_lifecycle_contract(monkeypatch):
    module = ModuleType("agent.subagent_lifecycle")
    module.SubagentLaunchRequest = _Request
    monkeypatch.setitem(sys.modules, "agent.subagent_lifecycle", module)


def test_worker_identity_survives_store_and_service_reconstruction(tmp_path):
    db_path = tmp_path / "durable.db"
    parent = SimpleNamespace(session_id="parent-1")

    lifecycle1 = _Lifecycle(["first report"])
    service1 = DurableWorkerService(
        DurableWorkerStore(db_path), lifecycle1, lambda: parent
    )
    worker = service1.create_worker(label="research")
    first = service1.send(worker["worker_id"], "inspect architecture")

    assert first["activation"]["status"] == "SUCCEEDED"
    first_activation = first["activation"]["activation_id"]
    first_subagent = first["activation"]["subagent_id"]

    lifecycle2 = _Lifecycle(["second report"])
    service2 = DurableWorkerService(
        DurableWorkerStore(db_path), lifecycle2, lambda: parent
    )
    second = service2.send(worker["worker_id"], "continue with the risks")

    assert second["activation"]["status"] == "SUCCEEDED"
    assert second["activation"]["activation_id"] != first_activation
    assert second["activation"]["subagent_id"] == "sa-1"
    assert first_subagent == "sa-1"
    assert "inspect architecture" in lifecycle2.requests[0].context
    assert "first report" in lifecycle2.requests[0].context
    assert service2.get_worker(worker["worker_id"])["status"] == "DORMANT"
    assert len(service2.store.list_activations("parent-1", worker["worker_id"])) == 2


def test_parent_authority_is_fail_closed(tmp_path):
    db_path = tmp_path / "durable.db"
    parent = SimpleNamespace(session_id="parent-1")
    service = DurableWorkerService(
        DurableWorkerStore(db_path), _Lifecycle(["ok"]), lambda: parent
    )
    worker = service.create_worker(label="security")

    other = DurableWorkerService(
        DurableWorkerStore(db_path), _Lifecycle(["should-not-run"]),
        lambda: SimpleNamespace(session_id="parent-2"),
    )
    with pytest.raises(DurableWorkerAuthorizationError):
        other.get_worker(worker["worker_id"])
    with pytest.raises(DurableWorkerAuthorizationError):
        other.send(worker["worker_id"], "steal work")


def test_inbox_idempotency_and_cold_pending_recovery(tmp_path):
    db_path = tmp_path / "durable.db"
    store = DurableWorkerStore(db_path)
    worker = store.create_worker("parent-1", label="worker")

    first = store.enqueue_message(
        "parent-1", worker["worker_id"], "hello", message_id="msg-fixed"
    )
    same = store.enqueue_message(
        "parent-1", worker["worker_id"], "hello", message_id="msg-fixed"
    )
    assert first["created"] is True
    assert same["created"] is False
    with pytest.raises(DurableWorkerConflictError):
        store.enqueue_message(
            "parent-1", worker["worker_id"], "different", message_id="msg-fixed"
        )

    reopened = DurableWorkerStore(db_path)
    claimed = reopened.claim_next_message("parent-1", worker["worker_id"])
    assert claimed["message_id"] == "msg-fixed"
    assert claimed["state"] == "PROCESSING"


def test_task_dag_readiness_cycle_guard_and_revision_cas(tmp_path):
    store = DurableWorkerStore(tmp_path / "durable.db")
    a = store.create_task("parent-1", subject="A")
    b = store.create_task("parent-1", subject="B")
    c = store.create_task("parent-1", subject="C")
    d = store.create_task("parent-1", subject="D")

    store.add_task_dependency("parent-1", b["task_id"], a["task_id"])
    store.add_task_dependency("parent-1", c["task_id"], a["task_id"])
    store.add_task_dependency("parent-1", d["task_id"], b["task_id"])
    store.add_task_dependency("parent-1", d["task_id"], c["task_id"])

    assert store.get_task("parent-1", a["task_id"])["ready"] is True
    assert store.get_task("parent-1", d["task_id"])["ready"] is False
    with pytest.raises(DurableWorkerConflictError):
        store.add_task_dependency("parent-1", a["task_id"], d["task_id"])

    current = store.get_task("parent-1", a["task_id"])
    completed = store.update_task(
        "parent-1",
        a["task_id"],
        status="completed",
        expected_revision=current["revision"],
    )
    assert completed["status"] == "completed"
    assert store.get_task("parent-1", b["task_id"])["ready"] is True
    with pytest.raises(DurableWorkerConflictError):
        store.update_task(
            "parent-1",
            a["task_id"],
            status="failed",
            expected_revision=current["revision"],
        )
