"""H5 bounded public graph projection tests."""
from __future__ import annotations

from agent.durable_task_public_graph import PublicDurableTaskGraphProjection
from agent.durable_task_orchestration import DurableTaskOrchestrator
from agent.durable_workers import DurableWorkerStore


def test_public_graph_truncates_large_run_text_without_changing_audit(tmp_path):
    store = DurableWorkerStore(tmp_path / "durable-workers.db")
    parent = "session-1"
    worker = store.create_worker(parent, label="worker")
    task = store.create_task(parent, subject="task", worker_id=worker["worker_id"])
    orchestrator = DurableTaskOrchestrator(store)
    reserved = orchestrator.reserve_ready_task(
        parent, task["task_id"], expected_revision=task["revision"]
    )
    huge = "x" * 5000
    store.finish_activation(
        parent,
        worker["worker_id"],
        reserved["activation_id"],
        reserved["message"]["message_id"],
        state="SUCCEEDED",
        summary=huge,
        message_state="CONSUMED",
        worker_state="DORMANT",
    )
    orchestrator.reconcile_result(
        parent,
        task["task_id"],
        reserved["activation_id"],
        {"status": "SUCCEEDED", "summary": huge},
    )

    graph = PublicDurableTaskGraphProjection(store.db_path).graph(parent)
    run = graph["tasks"][0]["last_run"]
    assert len(run["summary"]) == 2000
    assert run["summary_truncated"] is True

    with store._db() as db:
        stored = db.execute(
            "SELECT summary FROM durable_worker_task_runs WHERE activation_id=?",
            (reserved["activation_id"],),
        ).fetchone()[0]
    assert stored == huge


def test_public_graph_bounds_relation_lists_but_keeps_backend_ready_semantics(tmp_path):
    store = DurableWorkerStore(tmp_path / "durable-workers.db")
    parent = "session-1"
    orchestrator = DurableTaskOrchestrator(store)
    target = store.create_task(parent, subject="target")
    revision = target["revision"]
    blockers = []
    for index in range(70):
        blocker = store.create_task(parent, subject=f"blocker-{index}")
        blockers.append(blocker)
        updated = orchestrator.add_dependency(
            parent,
            target["task_id"],
            blocker["task_id"],
            expected_revision=revision,
        )
        revision = updated["revision"]

    graph = PublicDurableTaskGraphProjection(store.db_path).graph(parent)
    node = next(item for item in graph["tasks"] if item["task_id"] == target["task_id"])
    assert node["ready"] is False
    assert len(node["blocked_by"]) == 64
    assert node["blocked_by_truncated"] is True
