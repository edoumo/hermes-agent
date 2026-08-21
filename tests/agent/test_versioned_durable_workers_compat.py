"""Cross-phase compatibility smokes for the H6 versioned store."""
from __future__ import annotations

from agent.durable_task_orchestration import DurableTaskOrchestrator
from agent.versioned_durable_workers import VersionedDurableWorkerStore


def test_h1_activation_flow_runs_on_versioned_store(tmp_path):
    store = VersionedDurableWorkerStore(tmp_path / "durable-workers.db")
    worker = store.create_worker("session-1", label="worker")
    message = store.enqueue_message(
        "session-1",
        worker["worker_id"],
        "do one unit of work",
    )

    reserved = store.reserve_next_activation("session-1", worker["worker_id"])
    assert reserved["status"] == "RESERVED"
    assert reserved["message"]["message_id"] == message["message_id"]

    store.bind_activation(reserved["activation_id"], "sa-test")
    report = store.finish_activation(
        "session-1",
        worker["worker_id"],
        reserved["activation_id"],
        message["message_id"],
        state="SUCCEEDED",
        summary="done",
        message_state="CONSUMED",
        worker_state="DORMANT",
    )

    assert report is not None
    assert report["content"] == "done"
    assert store.get_worker("session-1", worker["worker_id"])["status"] == "DORMANT"
    messages = store.list_messages("session-1", worker["worker_id"])
    assert [item["state"] for item in messages] == ["CONSUMED", "COMPLETE"]


def test_h5_task_flow_runs_on_versioned_store(tmp_path):
    store = VersionedDurableWorkerStore(tmp_path / "durable-workers.db")
    worker = store.create_worker("session-1", label="task-worker")
    task = store.create_task(
        "session-1",
        subject="complete task",
        worker_id=worker["worker_id"],
    )
    orchestrator = DurableTaskOrchestrator(store)

    reserved = orchestrator.reserve_ready_task(
        "session-1",
        task["task_id"],
        expected_revision=task["revision"],
    )
    store.bind_activation(reserved["activation_id"], "sa-task-test")
    store.finish_activation(
        "session-1",
        worker["worker_id"],
        reserved["activation_id"],
        reserved["message"]["message_id"],
        state="SUCCEEDED",
        summary="task done",
        message_state="CONSUMED",
        worker_state="DORMANT",
    )
    reconciled = orchestrator.reconcile_result(
        "session-1",
        task["task_id"],
        reserved["activation_id"],
        {
            "status": "SUCCEEDED",
            "summary": "task done",
        },
    )

    assert reconciled["status"] == "completed"
    assert store.get_worker("session-1", worker["worker_id"])["status"] == "DORMANT"
