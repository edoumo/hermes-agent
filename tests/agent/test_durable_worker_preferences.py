"""H6.1 worker preference and archive lifecycle tests."""
from __future__ import annotations

import pytest

from agent.durable_worker_preferences import ManagedVersionedDurableWorkerStore
from agent.durable_workers import DurableWorkerConflictError


def _worker(tmp_path):
    store = ManagedVersionedDurableWorkerStore(tmp_path / "durable-workers.db")
    worker = store.create_worker(
        "session-1",
        label="worker",
        model="model-a",
        toolsets=["terminal"],
    )
    return store, worker


def test_preferences_update_model_label_and_toolsets_with_revision_cas(tmp_path):
    store, worker = _worker(tmp_path)

    updated = store.update_worker_preferences(
        "session-1",
        worker["worker_id"],
        expected_revision=worker["revision"],
        label="renamed",
        model="model-b",
        toolsets=["terminal", "web"],
    )

    assert updated["label"] == "renamed"
    assert updated["model"] == "model-b"
    assert updated["toolsets"] == ["terminal", "web"]
    assert updated["revision"] == worker["revision"] + 1

    with pytest.raises(DurableWorkerConflictError, match="revision conflict"):
        store.update_worker_preferences(
            "session-1",
            worker["worker_id"],
            expected_revision=worker["revision"],
            model="stale",
        )


def test_preferences_can_clear_model_override(tmp_path):
    store, worker = _worker(tmp_path)

    updated = store.update_worker_preferences(
        "session-1",
        worker["worker_id"],
        expected_revision=worker["revision"],
        model=None,
    )

    assert updated["model"] is None


def test_archive_preserves_worker_history_and_restore_is_reversible(tmp_path):
    store, worker = _worker(tmp_path)
    message = store.enqueue_message("session-1", worker["worker_id"], "hello")

    archived = store.set_worker_archived(
        "session-1",
        worker["worker_id"],
        archived=True,
        expected_revision=worker["revision"],
    )
    assert archived["status"] == "DISABLED"
    assert store.list_messages("session-1", worker["worker_id"])[0]["message_id"] == message["message_id"]

    restored = store.set_worker_archived(
        "session-1",
        worker["worker_id"],
        archived=False,
        expected_revision=archived["revision"],
    )
    assert restored["status"] == "DORMANT"
    assert restored["revision"] == archived["revision"] + 1
    assert store.list_messages("session-1", worker["worker_id"])[0]["message_id"] == message["message_id"]


def test_running_worker_cannot_be_edited_or_archived(tmp_path):
    store, worker = _worker(tmp_path)
    store.enqueue_message("session-1", worker["worker_id"], "run")
    reserved = store.reserve_next_activation("session-1", worker["worker_id"])
    assert reserved["status"] == "RESERVED"
    running = store.get_worker("session-1", worker["worker_id"])

    with pytest.raises(DurableWorkerConflictError, match="while it is running"):
        store.update_worker_preferences(
            "session-1",
            worker["worker_id"],
            expected_revision=running["revision"],
            model="model-c",
        )

    with pytest.raises(DurableWorkerConflictError, match="only a dormant"):
        store.set_worker_archived(
            "session-1",
            worker["worker_id"],
            archived=True,
            expected_revision=running["revision"],
        )


def test_failed_worker_cannot_be_archived_to_bypass_retry_recovery(tmp_path):
    store, worker = _worker(tmp_path)
    store.enqueue_message("session-1", worker["worker_id"], "run")
    reserved = store.reserve_next_activation("session-1", worker["worker_id"])
    store.finish_activation(
        "session-1",
        worker["worker_id"],
        reserved["activation_id"],
        reserved["message"]["message_id"],
        state="FAILED",
        error="boom",
        message_state="FAILED",
        worker_state="FAILED",
    )
    failed = store.get_worker("session-1", worker["worker_id"])

    with pytest.raises(DurableWorkerConflictError, match="only a dormant"):
        store.set_worker_archived(
            "session-1",
            worker["worker_id"],
            archived=True,
            expected_revision=failed["revision"],
        )
