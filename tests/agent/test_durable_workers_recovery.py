"""Crash recovery coverage for H1 durable workers."""
from agent.durable_workers import DurableWorkerStore


def test_abandoned_activation_is_requeued_after_process_loss(tmp_path, monkeypatch):
    db_path = tmp_path / "durable.db"
    store = DurableWorkerStore(db_path)
    worker = store.create_worker("parent-1", label="recover")
    message = store.enqueue_message("parent-1", worker["worker_id"], "resume me")
    reserved = store.reserve_next_activation("parent-1", worker["worker_id"])
    assert reserved["status"] == "RESERVED"
    assert reserved["message"]["message_id"] == message["message_id"]

    monkeypatch.setattr(
        "agent.durable_workers._owner_alive", lambda _pid, _started: False
    )
    reopened = DurableWorkerStore(db_path)

    activations = reopened.list_activations("parent-1", worker["worker_id"])
    assert activations[-1]["activation_id"] == reserved["activation_id"]
    assert activations[-1]["state"] == "ABANDONED"
    assert reopened.get_worker("parent-1", worker["worker_id"])["status"] == "DORMANT"
    requeued = reopened.reserve_next_activation("parent-1", worker["worker_id"])
    assert requeued["status"] == "RESERVED"
    assert requeued["message"]["message_id"] == message["message_id"]


def test_pid_reuse_marker_mismatch_is_treated_as_abandoned(tmp_path, monkeypatch):
    db_path = tmp_path / "durable.db"
    store = DurableWorkerStore(db_path)
    worker = store.create_worker("parent-1", label="pid-reuse")
    store.enqueue_message("parent-1", worker["worker_id"], "resume me")
    reserved = store.reserve_next_activation("parent-1", worker["worker_id"])

    observed = []

    def fake_alive(pid, started_at):
        observed.append((pid, started_at))
        return False

    monkeypatch.setattr("agent.durable_workers._owner_alive", fake_alive)
    reopened = DurableWorkerStore(db_path)
    assert observed
    activation = reopened.list_activations("parent-1", worker["worker_id"])[-1]
    assert activation["activation_id"] == reserved["activation_id"]
    assert activation["state"] == "ABANDONED"
