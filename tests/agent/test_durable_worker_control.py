"""H4 durable worker operator-control state-machine tests."""
from __future__ import annotations

import pytest

from agent.durable_worker_control import DurableWorkerControl
from agent.durable_workers import DurableWorkerConflictError, DurableWorkerStore


def _store(tmp_path):
    return DurableWorkerStore(tmp_path / "durable-workers-h4.db")


def _reserved(store, parent="session-a", label="worker-a"):
    worker = store.create_worker(parent, label=label)
    message = store.enqueue_message(parent, worker["worker_id"], "do the work")
    reservation = store.reserve_next_activation(parent, worker["worker_id"])
    assert reservation["status"] == "RESERVED"
    return worker, message, reservation


def test_cancel_request_keeps_worker_and_message_locked_until_terminal(tmp_path):
    store = _store(tmp_path)
    worker, message, reservation = _reserved(store)
    worker_id = worker["worker_id"]
    activation_id = reservation["activation_id"]
    control = DurableWorkerControl(store)

    requested = control.request_cancel("session-a", worker_id, activation_id)

    assert requested["changed"] is True
    assert requested["previous_state"] == "STARTING"
    assert requested["state"] == "CANCEL_REQUESTED"
    assert store.get_worker("session-a", worker_id)["status"] == "RUNNING"
    messages = store.list_messages("session-a", worker_id)
    assert next(m for m in messages if m["message_id"] == message["message_id"])["state"] == "PROCESSING"

    repeated = control.request_cancel("session-a", worker_id, activation_id)
    assert repeated["changed"] is False
    assert repeated["state"] == "CANCEL_REQUESTED"


def test_rejected_cancel_can_restore_previous_running_state_with_cas(tmp_path):
    store = _store(tmp_path)
    worker, _message, reservation = _reserved(store)
    worker_id = worker["worker_id"]
    activation_id = reservation["activation_id"]
    store.bind_activation(activation_id, "sa-1")
    control = DurableWorkerControl(store)

    requested = control.request_cancel("session-a", worker_id, activation_id)
    assert requested["previous_state"] == "RUNNING"
    assert control.restore_cancel_request(
        "session-a",
        worker_id,
        activation_id,
        previous_state="RUNNING",
    ) is True
    assert control.get_activation("session-a", worker_id, activation_id)["state"] == "RUNNING"

    # CAS is idempotently safe: the marker is already gone.
    assert control.restore_cancel_request(
        "session-a",
        worker_id,
        activation_id,
        previous_state="RUNNING",
    ) is False


def test_cancel_rejects_noncurrent_or_terminal_activation(tmp_path):
    store = _store(tmp_path)
    worker, message, reservation = _reserved(store)
    worker_id = worker["worker_id"]
    activation_id = reservation["activation_id"]
    store.finish_activation(
        "session-a",
        worker_id,
        activation_id,
        message["message_id"],
        state="FAILED",
        error="boom",
        message_state="FAILED",
        worker_state="FAILED",
    )
    control = DurableWorkerControl(store)

    with pytest.raises(DurableWorkerConflictError, match="not running"):
        control.request_cancel("session-a", worker_id, activation_id)


def test_retry_failed_worker_requeues_message_without_rewriting_history(tmp_path):
    store = _store(tmp_path)
    worker, message, reservation = _reserved(store)
    worker_id = worker["worker_id"]
    activation_id = reservation["activation_id"]
    store.bind_activation(activation_id, "sa-failed")
    store.finish_activation(
        "session-a",
        worker_id,
        activation_id,
        message["message_id"],
        state="FAILED",
        error="provider failed",
        message_state="FAILED",
        worker_state="FAILED",
    )
    before = store.get_worker("session-a", worker_id)
    control = DurableWorkerControl(store)

    result = control.retry_failed_worker(
        "session-a", worker_id, expected_revision=before["revision"]
    )

    assert result["status"] == "RETRY_READY"
    assert result["message_id"] == message["message_id"]
    assert result["previous_activation_id"] == activation_id
    assert result["worker"]["status"] == "DORMANT"
    assert result["worker"]["revision"] == before["revision"] + 1
    messages = store.list_messages("session-a", worker_id)
    retried = next(m for m in messages if m["message_id"] == message["message_id"])
    assert retried["state"] == "PENDING"
    activations = store.list_activations("session-a", worker_id)
    historical = next(a for a in activations if a["activation_id"] == activation_id)
    assert historical["state"] == "FAILED"
    assert historical["error"] == "provider failed"


def test_retry_failed_worker_enforces_revision_and_failed_state(tmp_path):
    store = _store(tmp_path)
    worker, message, reservation = _reserved(store)
    worker_id = worker["worker_id"]
    activation_id = reservation["activation_id"]
    store.finish_activation(
        "session-a",
        worker_id,
        activation_id,
        message["message_id"],
        state="FAILED",
        error="boom",
        message_state="FAILED",
        worker_state="FAILED",
    )
    control = DurableWorkerControl(store)
    current = store.get_worker("session-a", worker_id)

    with pytest.raises(DurableWorkerConflictError, match="revision changed"):
        control.retry_failed_worker(
            "session-a", worker_id, expected_revision=current["revision"] - 1
        )

    control.retry_failed_worker(
        "session-a", worker_id, expected_revision=current["revision"]
    )
    with pytest.raises(DurableWorkerConflictError, match="requires FAILED"):
        control.retry_failed_worker("session-a", worker_id)


def test_session_summary_is_parent_scoped(tmp_path):
    store = _store(tmp_path)
    worker_a, _message_a, reservation_a = _reserved(store, "session-a", "worker-a")
    store.bind_activation(reservation_a["activation_id"], "sa-a")

    worker_b = store.create_worker("session-b", label="worker-b")
    store.enqueue_message("session-b", worker_b["worker_id"], "other tenant work")

    summary = DurableWorkerControl(store).session_summary("session-a")

    assert summary["workers"] == {
        "total": 1,
        "DORMANT": 0,
        "RUNNING": 1,
        "FAILED": 0,
        "DISABLED": 0,
    }
    assert summary["activations"]["RUNNING"] == 1
    assert summary["messages"]["PROCESSING"] == 1
    assert summary["messages"]["PENDING"] == 0
