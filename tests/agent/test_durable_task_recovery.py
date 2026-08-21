"""H5 task-aware retry/cancel recovery tests."""
from __future__ import annotations

import pytest

from agent.durable_task_recovery import RecoverableDurableTaskOrchestrator
from agent.durable_workers import DurableWorkerConflictError, DurableWorkerStore


def _setup(tmp_path, subject="task"):
    store = DurableWorkerStore(tmp_path / "durable-workers.db")
    parent = "session-1"
    worker = store.create_worker(parent, label="worker")
    task = store.create_task(
        parent,
        subject=subject,
        worker_id=worker["worker_id"],
    )
    return store, RecoverableDurableTaskOrchestrator(store), parent, worker, task


def test_operator_cancel_redispatch_reuses_same_message_with_new_activation(tmp_path):
    store, orchestrator, parent, worker, task = _setup(tmp_path, "cancel task")
    first = orchestrator.reserve_ready_task(
        parent, task["task_id"], expected_revision=task["revision"]
    )
    store.finish_activation(
        parent,
        worker["worker_id"],
        first["activation_id"],
        first["message"]["message_id"],
        state="CANCELLED",
        error="operator cancellation completed",
        message_state="PENDING",
        worker_state="DORMANT",
    )
    pending = orchestrator.reconcile_result(
        parent,
        task["task_id"],
        first["activation_id"],
        {"status": "CANCELLED", "retryable": True},
    )
    assert pending["status"] == "pending"

    second = orchestrator.reserve_ready_task(
        parent,
        task["task_id"],
        expected_revision=pending["revision"],
    )
    assert second["reused_message"] is True
    assert second["message"]["message_id"] == first["message"]["message_id"]
    assert second["activation_id"] != first["activation_id"]


def test_failed_task_recovery_restores_worker_message_and_redispatches(tmp_path):
    store, orchestrator, parent, worker, task = _setup(tmp_path, "failed task")
    first = orchestrator.reserve_ready_task(
        parent, task["task_id"], expected_revision=task["revision"]
    )
    store.finish_activation(
        parent,
        worker["worker_id"],
        first["activation_id"],
        first["message"]["message_id"],
        state="FAILED",
        error="boom",
        message_state="FAILED",
        worker_state="FAILED",
    )
    failed = orchestrator.reconcile_result(
        parent,
        task["task_id"],
        first["activation_id"],
        {"status": "FAILED", "error": "boom"},
    )
    assert failed["status"] == "failed"

    recovered = orchestrator.recover_failed_task(
        parent,
        task["task_id"],
        expected_revision=failed["revision"],
    )
    assert recovered["status"] == "RECOVERY_READY"
    assert recovered["task"]["status"] == "pending"
    assert recovered["worker"]["status"] == "DORMANT"
    assert recovered["message_id"] == first["message"]["message_id"]

    second = orchestrator.reserve_ready_task(
        parent,
        task["task_id"],
        expected_revision=recovered["task"]["revision"],
    )
    assert second["reused_message"] is True
    assert second["message"]["message_id"] == recovered["message_id"]
    assert second["activation_id"] != recovered["previous_activation_id"]


def test_recovery_interoperates_with_h4_style_worker_retry(tmp_path):
    store, orchestrator, parent, worker, task = _setup(tmp_path, "interop task")
    first = orchestrator.reserve_ready_task(
        parent, task["task_id"], expected_revision=task["revision"]
    )
    store.finish_activation(
        parent,
        worker["worker_id"],
        first["activation_id"],
        first["message"]["message_id"],
        state="FAILED",
        error="boom",
        message_state="FAILED",
        worker_state="FAILED",
    )
    failed = orchestrator.reconcile_result(
        parent,
        task["task_id"],
        first["activation_id"],
        {"status": "FAILED", "error": "boom"},
    )

    # Simulate H4 retry having already restored worker + durable message.
    with store._db() as db:
        db.execute(
            "UPDATE durable_worker_messages SET state='PENDING' WHERE message_id=?",
            (first["message"]["message_id"],),
        )
        db.execute(
            "UPDATE durable_workers SET status='DORMANT',revision=revision+1 "
            "WHERE worker_id=?",
            (worker["worker_id"],),
        )

    recovered = orchestrator.recover_failed_task(
        parent,
        task["task_id"],
        expected_revision=failed["revision"],
    )
    assert recovered["task"]["status"] == "pending"
    assert recovered["worker"]["status"] == "DORMANT"


def test_redispatch_rejects_unrelated_pending_inbox(tmp_path):
    store, orchestrator, parent, worker, task = _setup(tmp_path, "guard task")
    store.enqueue_message(parent, worker["worker_id"], "unrelated")
    with pytest.raises(DurableWorkerConflictError, match="unrelated pending"):
        orchestrator.reserve_ready_task(
            parent,
            task["task_id"],
            expected_revision=task["revision"],
        )
