"""H5 Durable Task orchestration unit tests."""
from __future__ import annotations

import pytest

from agent.durable_task_orchestration import (
    DurableTaskGraphProjection,
    DurableTaskOrchestrator,
)
from agent.durable_workers import DurableWorkerConflictError, DurableWorkerStore


def _store(tmp_path):
    return DurableWorkerStore(tmp_path / "durable-workers.db")


def test_h5_edits_assignment_and_dependencies_with_revision_cas(tmp_path):
    store = _store(tmp_path)
    orchestrator = DurableTaskOrchestrator(store)
    parent = "session-1"
    worker = store.create_worker(parent, label="worker")
    first = store.create_task(parent, subject="first")
    second = store.create_task(parent, subject="second")

    edited = orchestrator.edit_task(
        parent,
        first["task_id"],
        expected_revision=first["revision"],
        description="updated",
        worker_id=worker["worker_id"],
        worker_id_present=True,
    )
    assert edited["worker_id"] == worker["worker_id"]
    assert edited["description"] == "updated"
    assert edited["revision"] == first["revision"] + 1

    dependent = orchestrator.add_dependency(
        parent,
        second["task_id"],
        first["task_id"],
        expected_revision=second["revision"],
    )
    assert dependent["blocked_by"] == [first["task_id"]]
    assert dependent["ready"] is False
    assert dependent["revision"] == second["revision"] + 1

    with pytest.raises(DurableWorkerConflictError, match="cycle"):
        orchestrator.add_dependency(
            parent,
            first["task_id"],
            second["task_id"],
            expected_revision=edited["revision"],
        )

    removed = orchestrator.remove_dependency(
        parent,
        second["task_id"],
        first["task_id"],
        expected_revision=dependent["revision"],
    )
    assert removed["blocked_by"] == []
    assert removed["ready"] is True
    assert removed["changed"] is True


def test_h5_dispatch_atomically_reserves_ready_task_and_reconciles_success(tmp_path):
    store = _store(tmp_path)
    orchestrator = DurableTaskOrchestrator(store)
    parent = "session-1"
    worker = store.create_worker(parent, label="worker")
    task = store.create_task(
        parent,
        subject="do work",
        description="return marker",
        worker_id=worker["worker_id"],
    )

    reserved = orchestrator.reserve_ready_task(
        parent, task["task_id"], expected_revision=task["revision"]
    )

    assert reserved["status"] == "RESERVED"
    assert reserved["task_id"] == task["task_id"]
    assert reserved["message"]["state"] == "PROCESSING"
    assert store.get_worker(parent, worker["worker_id"])["status"] == "RUNNING"
    running_task = store.get_task(parent, task["task_id"])
    assert running_task["status"] == "in_progress"
    assert running_task["revision"] == task["revision"] + 1

    activation_id = reserved["activation_id"]
    message_id = reserved["message"]["message_id"]
    store.bind_activation(activation_id, "sa-1")
    store.finish_activation(
        parent,
        worker["worker_id"],
        activation_id,
        message_id,
        state="SUCCEEDED",
        summary="done",
        message_state="CONSUMED",
        worker_state="DORMANT",
    )
    reconciled = orchestrator.reconcile_result(
        parent,
        task["task_id"],
        activation_id,
        {"status": "SUCCEEDED", "summary": "done"},
    )
    assert reconciled["status"] == "completed"

    graph = DurableTaskGraphProjection(store.db_path).graph(parent)
    node = next(item for item in graph["tasks"] if item["task_id"] == task["task_id"])
    assert node["last_run"]["activation_id"] == activation_id
    assert node["last_run"]["state"] == "SUCCEEDED"
    assert graph["counts"]["completed"] == 1


def test_h5_operator_cancel_requeues_task_but_system_failure_marks_failed(tmp_path):
    store = _store(tmp_path)
    orchestrator = DurableTaskOrchestrator(store)
    parent = "session-1"

    worker1 = store.create_worker(parent, label="cancel-worker")
    task1 = store.create_task(
        parent, subject="cancel me", worker_id=worker1["worker_id"]
    )
    reserved1 = orchestrator.reserve_ready_task(
        parent, task1["task_id"], expected_revision=task1["revision"]
    )
    store.finish_activation(
        parent,
        worker1["worker_id"],
        reserved1["activation_id"],
        reserved1["message"]["message_id"],
        state="CANCELLED",
        error="operator cancellation completed",
        message_state="PENDING",
        worker_state="DORMANT",
    )
    cancelled = orchestrator.reconcile_result(
        parent,
        task1["task_id"],
        reserved1["activation_id"],
        {"status": "CANCELLED", "retryable": True},
    )
    assert cancelled["status"] == "pending"
    assert cancelled["ready"] is True

    # Clear the requeued durable inbox before using another task/worker pair.
    worker2 = store.create_worker(parent, label="failure-worker")
    task2 = store.create_task(
        parent, subject="fail me", worker_id=worker2["worker_id"]
    )
    reserved2 = orchestrator.reserve_ready_task(
        parent, task2["task_id"], expected_revision=task2["revision"]
    )
    store.finish_activation(
        parent,
        worker2["worker_id"],
        reserved2["activation_id"],
        reserved2["message"]["message_id"],
        state="FAILED",
        error="boom",
        message_state="FAILED",
        worker_state="FAILED",
    )
    failed = orchestrator.reconcile_result(
        parent,
        task2["task_id"],
        reserved2["activation_id"],
        {"status": "FAILED", "error": "boom"},
    )
    assert failed["status"] == "failed"


def test_h5_dispatch_rejects_blocked_unassigned_and_backlogged_workers(tmp_path):
    store = _store(tmp_path)
    orchestrator = DurableTaskOrchestrator(store)
    parent = "session-1"
    worker = store.create_worker(parent, label="worker")
    blocker = store.create_task(parent, subject="blocker")
    target = store.create_task(
        parent, subject="target", worker_id=worker["worker_id"]
    )
    target = orchestrator.add_dependency(
        parent,
        target["task_id"],
        blocker["task_id"],
        expected_revision=target["revision"],
    )

    with pytest.raises(DurableWorkerConflictError, match="blocked"):
        orchestrator.reserve_ready_task(
            parent, target["task_id"], expected_revision=target["revision"]
        )

    blocker = store.update_task(
        parent,
        blocker["task_id"],
        status="completed",
        expected_revision=blocker["revision"],
    )
    assert blocker["status"] == "completed"

    store.enqueue_message(parent, worker["worker_id"], "older durable message")
    current = store.get_task(parent, target["task_id"])
    with pytest.raises(DurableWorkerConflictError, match="pending durable inbox"):
        orchestrator.reserve_ready_task(
            parent, target["task_id"], expected_revision=current["revision"]
        )

    unassigned = store.create_task(parent, subject="unassigned")
    with pytest.raises(DurableWorkerConflictError, match="assigned worker"):
        orchestrator.reserve_ready_task(
            parent,
            unassigned["task_id"],
            expected_revision=unassigned["revision"],
        )


def test_h5_graph_is_session_scoped_and_exposes_edges_without_host_details(tmp_path):
    store = _store(tmp_path)
    orchestrator = DurableTaskOrchestrator(store)
    a = "session-a"
    b = "session-b"
    first = store.create_task(a, subject="A1")
    second = store.create_task(a, subject="A2")
    foreign = store.create_task(b, subject="B1")
    second = orchestrator.add_dependency(
        a,
        second["task_id"],
        first["task_id"],
        expected_revision=second["revision"],
    )

    graph = DurableTaskGraphProjection(store.db_path).graph(a)
    ids = {item["task_id"] for item in graph["tasks"]}
    assert ids == {first["task_id"], second["task_id"]}
    assert foreign["task_id"] not in ids
    assert graph["edges"] == [{"from": first["task_id"], "to": second["task_id"]}]
    assert graph["counts"]["blocked"] == 1
    assert "owner_pid" not in str(graph)
    assert "owner_started_at" not in str(graph)
