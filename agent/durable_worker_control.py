"""H4 operational control primitives for Hermes Durable Workers.

The H1 store remains the durable source of truth.  This module adds explicit
operator transitions without serializing live lifecycle objects or weakening
session ownership.  Every mutation is session scoped and transactional.
"""
from __future__ import annotations

from typing import Any, Optional

from agent.durable_workers import (
    DurableWorkerConflictError,
    DurableWorkerError,
    DurableWorkerStore,
    _now,
)

_ACTIVE_ACTIVATION_STATES = {"STARTING", "RUNNING", "CANCEL_REQUESTED"}
_CANCELABLE_ACTIVATION_STATES = {"STARTING", "RUNNING"}


class DurableWorkerControl:
    """Transactional operator controls layered on :class:`DurableWorkerStore`."""

    def __init__(self, store: DurableWorkerStore):
        self.store = store

    def get_activation(
        self, parent: str, worker_id: str, activation_id: str
    ) -> dict[str, Any]:
        with self.store._db() as db:
            self.store._owned_worker(db, parent, worker_id)
            row = db.execute(
                "SELECT * FROM durable_worker_activations "
                "WHERE activation_id=? AND worker_id=?",
                (activation_id, worker_id),
            ).fetchone()
            if row is None:
                raise DurableWorkerConflictError("activation not found for worker")
            return dict(row)

    def is_cancel_requested(
        self, parent: str, worker_id: str, activation_id: str
    ) -> bool:
        return (
            self.get_activation(parent, worker_id, activation_id).get("state")
            == "CANCEL_REQUESTED"
        )

    def request_cancel(
        self, parent: str, worker_id: str, activation_id: str
    ) -> dict[str, Any]:
        """Persist operator cancellation intent while keeping the worker locked.

        This method does not claim that the child stopped.  The worker remains
        RUNNING and its message remains PROCESSING until lifecycle terminality
        is observed or abandoned-work recovery proves the owner disappeared.
        """
        db = self.store._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            worker = self.store._owned_worker(db, parent, worker_id)
            row = db.execute(
                "SELECT * FROM durable_worker_activations "
                "WHERE activation_id=? AND worker_id=?",
                (activation_id, worker_id),
            ).fetchone()
            if row is None:
                raise DurableWorkerConflictError("activation not found for worker")
            if worker["last_activation_id"] != activation_id:
                raise DurableWorkerConflictError(
                    "activation is not the worker's current activation"
                )
            if worker["status"] != "RUNNING":
                raise DurableWorkerConflictError(
                    "worker is not running an activation"
                )
            state = str(row["state"] or "")
            if state == "CANCEL_REQUESTED":
                db.commit()
                return {**dict(row), "changed": False}
            if state not in _CANCELABLE_ACTIVATION_STATES:
                raise DurableWorkerConflictError(
                    f"activation cannot be cancelled from state {state or 'UNKNOWN'}"
                )
            updated = db.execute(
                "UPDATE durable_worker_activations "
                "SET state='CANCEL_REQUESTED' "
                "WHERE activation_id=? AND worker_id=? AND state=?",
                (activation_id, worker_id, state),
            ).rowcount
            if updated != 1:
                raise DurableWorkerConflictError(
                    "activation state changed while requesting cancellation"
                )
            db.commit()
            current = dict(row)
            current["state"] = "CANCEL_REQUESTED"
            current["changed"] = True
            current["previous_state"] = state
            return current
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def restore_cancel_request(
        self,
        parent: str,
        worker_id: str,
        activation_id: str,
        *,
        previous_state: str,
    ) -> bool:
        """Best-effort CAS rollback when lifecycle rejects a cancellation."""
        if previous_state not in _CANCELABLE_ACTIVATION_STATES:
            raise DurableWorkerError("invalid cancellation rollback state")
        with self.store._db() as db:
            worker = self.store._owned_worker(db, parent, worker_id)
            if (
                worker["status"] != "RUNNING"
                or worker["last_activation_id"] != activation_id
            ):
                return False
            updated = db.execute(
                "UPDATE durable_worker_activations SET state=? "
                "WHERE activation_id=? AND worker_id=? "
                "AND state='CANCEL_REQUESTED'",
                (previous_state, activation_id, worker_id),
            ).rowcount
            return updated == 1

    def retry_failed_worker(
        self,
        parent: str,
        worker_id: str,
        *,
        expected_revision: Optional[int] = None,
    ) -> dict[str, Any]:
        """Requeue the message from the worker's last terminal failed activation.

        Historical activation rows are immutable audit records.  Retry only
        moves its parent message FAILED -> PENDING and worker FAILED -> DORMANT.
        A revision CAS prevents stale operator actions.
        """
        if expected_revision is not None and type(expected_revision) is not int:
            raise DurableWorkerError("expected_revision must be an integer or null")
        db = self.store._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            worker = self.store._owned_worker(db, parent, worker_id)
            if expected_revision is not None and worker["revision"] != expected_revision:
                raise DurableWorkerConflictError(
                    f"worker revision changed (expected {expected_revision}, "
                    f"actual {worker['revision']})"
                )
            if worker["status"] != "FAILED":
                raise DurableWorkerConflictError(
                    "worker retry requires FAILED state"
                )
            activation_id = str(worker["last_activation_id"] or "").strip()
            if not activation_id:
                raise DurableWorkerConflictError(
                    "failed worker has no last activation"
                )
            activation = db.execute(
                "SELECT * FROM durable_worker_activations "
                "WHERE activation_id=? AND worker_id=?",
                (activation_id, worker_id),
            ).fetchone()
            if activation is None:
                raise DurableWorkerConflictError(
                    "failed worker activation is missing"
                )
            activation_state = str(activation["state"] or "")
            if activation_state in _ACTIVE_ACTIVATION_STATES:
                raise DurableWorkerConflictError(
                    "failed worker still has a non-terminal activation"
                )
            if activation_state == "SUCCEEDED":
                raise DurableWorkerConflictError(
                    "successful activation cannot be retried as failure"
                )
            message_id = str(activation["message_id"] or "").strip()
            if not message_id:
                raise DurableWorkerConflictError(
                    "failed activation has no retryable message"
                )
            message = db.execute(
                "SELECT * FROM durable_worker_messages "
                "WHERE message_id=? AND worker_id=? AND direction='parent'",
                (message_id, worker_id),
            ).fetchone()
            if message is None or message["state"] != "FAILED":
                raise DurableWorkerConflictError(
                    "failed activation message is not in FAILED state"
                )

            now = _now()
            updated_message = db.execute(
                "UPDATE durable_worker_messages "
                "SET state='PENDING', updated_at=? "
                "WHERE message_id=? AND worker_id=? AND state='FAILED'",
                (now, message_id, worker_id),
            ).rowcount
            if updated_message != 1:
                raise DurableWorkerConflictError(
                    "retry message state changed concurrently"
                )
            updated_worker = db.execute(
                "UPDATE durable_workers "
                "SET status='DORMANT', updated_at=?, revision=revision+1 "
                "WHERE worker_id=? AND parent_session_id=? AND status='FAILED'",
                (now, worker_id, parent),
            ).rowcount
            if updated_worker != 1:
                raise DurableWorkerConflictError(
                    "worker state changed concurrently during retry"
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        return {
            "worker": self.store.get_worker(parent, worker_id),
            "message_id": message_id,
            "previous_activation_id": activation_id,
            "status": "RETRY_READY",
        }

    def session_summary(self, parent: str) -> dict[str, Any]:
        """Return session-scoped operational counts without cross-session usage."""
        with self.store._db() as db:
            worker_rows = db.execute(
                "SELECT status,COUNT(*) AS count FROM durable_workers "
                "WHERE parent_session_id=? GROUP BY status",
                (parent,),
            ).fetchall()
            activation_rows = db.execute(
                "SELECT a.state,COUNT(*) AS count "
                "FROM durable_worker_activations a "
                "JOIN durable_workers w ON w.worker_id=a.worker_id "
                "WHERE w.parent_session_id=? "
                "AND a.state IN('STARTING','RUNNING','CANCEL_REQUESTED') "
                "GROUP BY a.state",
                (parent,),
            ).fetchall()
            message_rows = db.execute(
                "SELECT m.state,COUNT(*) AS count "
                "FROM durable_worker_messages m "
                "JOIN durable_workers w ON w.worker_id=m.worker_id "
                "WHERE w.parent_session_id=? AND m.direction='parent' "
                "AND m.state IN('PENDING','PROCESSING','FAILED') "
                "GROUP BY m.state",
                (parent,),
            ).fetchall()

        workers = {str(row["status"]): int(row["count"]) for row in worker_rows}
        activations = {
            str(row["state"]): int(row["count"]) for row in activation_rows
        }
        messages = {str(row["state"]): int(row["count"]) for row in message_rows}
        return {
            "workers": {
                "total": sum(workers.values()),
                "DORMANT": workers.get("DORMANT", 0),
                "RUNNING": workers.get("RUNNING", 0),
                "FAILED": workers.get("FAILED", 0),
                "DISABLED": workers.get("DISABLED", 0),
            },
            "activations": {
                "STARTING": activations.get("STARTING", 0),
                "RUNNING": activations.get("RUNNING", 0),
                "CANCEL_REQUESTED": activations.get("CANCEL_REQUESTED", 0),
            },
            "messages": {
                "PENDING": messages.get("PENDING", 0),
                "PROCESSING": messages.get("PROCESSING", 0),
                "FAILED": messages.get("FAILED", 0),
            },
        }


__all__ = ["DurableWorkerControl"]
