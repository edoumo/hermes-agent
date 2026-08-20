"""Crash recovery coverage for H1 durable workers."""

from agent.durable_workers import DurableWorkerStore


def test_abandoned_activation_is_requeued_after_process_loss(tmp_path, monkeypatch):
    db_path = tmp_path / "durable.db"
    store = DurableWorkerStore(db_path)
    worker = store.create_worker("parent-1", label="recover")
    msg = store.enqueue_message("parent-1", worker["worker_id"], "resume me")
    claimed = store.claim_next_message("parent-1", worker["worker_id"])
    assert claimed["state"] == "PROCESSING"
    activation_id = store.start_activation(
        "parent-1", worker["worker_id"], msg["message_id"]
    )

    monkeypatch.setattr("agent.durable_workers._alive", lambda _pid: False)
    reopened = DurableWorkerStore(db_path)

    recovered = reopened.list_activations("parent-1", worker["worker_id"])
    assert recovered[-1]["activation_id"] == activation_id
    assert recovered[-1]["state"] == "ABANDONED"
    assert reopened.get_worker("parent-1", worker["worker_id"])["status"] == "DORMANT"
    requeued = reopened.claim_next_message("parent-1", worker["worker_id"])
    assert requeued["message_id"] == msg["message_id"]
