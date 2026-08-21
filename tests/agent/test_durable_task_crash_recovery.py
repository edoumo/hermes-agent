"""H5 process-boundary task reconciliation tests."""
from __future__ import annotations

from agent.durable_task_recovery import RecoverableDurableTaskOrchestrator
from agent.durable_workers import DurableWorkerStore


def _setup(tmp_path, subject):
    path = tmp_path / "durable-workers.db"
    store = DurableWorkerStore(path)
    parent = "session-1"
    worker = store.create_worker(parent, label="worker")
    task = store.create_task(parent, subject=subject, worker_id=worker["worker_id"])
    orchestrator = RecoverableDurableTaskOrchestrator(store)
    reserved = orchestrator.reserve_ready_task(
        parent, task["task_id"], expected_revision=task["revision"]
    )
    return path, store, parent, worker, task, reserved


def test_startup_reconciliation_projects_unseen_success_to_completed(tmp_path):
    path, store, parent, worker, task, reserved = _setup(tmp_path, "success")
    store.finish_activation(
        parent,
        worker["worker_id"],
        reserved["activation_id"],
        reserved["message"]["message_id"],
        state="SUCCEEDED",
        summary="done",
        message_state="CONSUMED",
        worker_state="DORMANT",
    )
    assert store.get_task(parent, task["task_id"])["status"] == "in_progress"

    restarted = DurableWorkerStore(path)
    RecoverableDurableTaskOrchestrator(restarted)
    assert restarted.get_task(parent, task["task_id"])["status"] == "completed"


def test_startup_reconciliation_projects_h1_abandoned_recovery_to_pending(tmp_path):
    path, store, parent, worker, task, reserved = _setup(tmp_path, "abandoned")
    with store._db() as db:
        db.execute(
            "UPDATE durable_worker_activations SET owner_pid=?,owner_started_at=? "
            "WHERE activation_id=?",
            (99999999, 1, reserved["activation_id"]),
        )

    restarted = DurableWorkerStore(path)
    activation = restarted.list_activations(parent, worker["worker_id"])[-1]
    assert activation["state"] == "ABANDONED"
    message = next(
        item
        for item in restarted.list_messages(parent, worker["worker_id"])
        if item["message_id"] == reserved["message"]["message_id"]
    )
    assert message["state"] == "PENDING"
    assert restarted.get_worker(parent, worker["worker_id"])["status"] == "DORMANT"

    RecoverableDurableTaskOrchestrator(restarted)
    assert restarted.get_task(parent, task["task_id"])["status"] == "pending"


def test_startup_reconciliation_projects_unseen_fail_closed_state_to_failed(tmp_path):
    path, store, parent, worker, task, reserved = _setup(tmp_path, "failed")
    store.finish_activation(
        parent,
        worker["worker_id"],
        reserved["activation_id"],
        reserved["message"]["message_id"],
        state="FAILED",
        error="boom",
        message_state="FAILED",
        worker_state="FAILED",
    )
    assert store.get_task(parent, task["task_id"])["status"] == "in_progress"

    restarted = DurableWorkerStore(path)
    RecoverableDurableTaskOrchestrator(restarted)
    assert restarted.get_task(parent, task["task_id"])["status"] == "failed"
