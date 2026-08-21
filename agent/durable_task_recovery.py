"""H5 retry/cancel/crash recovery for Durable Task orchestration.

A task-dispatched durable message must remain associated with the task when H4
requeues it after operator cancellation, after a failed-worker recovery, or
after process-boundary recovery. This layer extends the base H5 orchestrator so
redispatch reuses that same task message while creating a fresh activation id.
"""
from __future__ import annotations

import os
from typing import Any

from agent.durable_task_orchestration import DurableTaskOrchestrator
from agent.durable_workers import (
    DurableWorkerConflictError,
    _id,
    _now,
    _process_start_time,
)


class RecoverableDurableTaskOrchestrator(DurableTaskOrchestrator):
    """H5 orchestrator with task-aware failed/crash recovery and message reuse."""

    def __init__(self, store):
        super().__init__(store)
        self.recover_task_runs()

    def recover_task_runs(self) -> int:
        """Align stale in-progress tasks with already-durable H1/H4 terminal state.

        DurableWorkerStore is constructed before this orchestrator, so H1 has
        already converted dead-owner activations to ABANDONED and restored the
        worker/message pair. H5 never guesses from process state here; it only
        projects those persisted states back onto the latest task run.
        """
        recovered = 0
        db = self.store._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT r.activation_id,r.task_id,r.message_id,r.worker_id,"
                "a.state AS activation_state,a.completed_at AS activation_completed_at,"
                "a.summary AS activation_summary,a.error AS activation_error,"
                "m.state AS message_state,w.status AS worker_status,t.parent_session_id "
                "FROM durable_worker_task_runs r "
                "JOIN durable_worker_tasks t ON t.task_id=r.task_id "
                "JOIN durable_worker_activations a ON a.activation_id=r.activation_id "
                "JOIN durable_worker_messages m ON m.message_id=r.message_id "
                "JOIN durable_workers w ON w.worker_id=r.worker_id "
                "WHERE t.status='in_progress'"
            ).fetchall()
            for row in rows:
                state = str(row["activation_state"] or "")
                if state in {"STARTING", "RUNNING", "CANCEL_REQUESTED"}:
                    continue
                latest = db.execute(
                    "SELECT activation_id FROM durable_worker_task_runs "
                    "WHERE task_id=? ORDER BY created_at DESC,activation_id DESC LIMIT 1",
                    (row["task_id"],),
                ).fetchone()
                if latest is None or latest["activation_id"] != row["activation_id"]:
                    continue

                message_state = str(row["message_state"] or "")
                worker_state = str(row["worker_status"] or "")
                target = None
                if (
                    state == "SUCCEEDED"
                    and message_state == "CONSUMED"
                    and worker_state == "DORMANT"
                ):
                    target = "completed"
                elif (
                    state in {"FAILED_TO_START", "ABANDONED", "CANCELLED"}
                    and message_state == "PENDING"
                    and worker_state == "DORMANT"
                ):
                    target = "pending"
                elif (
                    message_state == "FAILED"
                    or worker_state == "FAILED"
                    or state in {"FAILED", "INTERRUPTED", "CANCELLED"}
                ):
                    target = "failed"
                if target is None:
                    continue

                db.execute(
                    "UPDATE durable_worker_task_runs SET state=?,completed_at=?,summary=?,error=? "
                    "WHERE activation_id=?",
                    (
                        state,
                        row["activation_completed_at"],
                        row["activation_summary"],
                        row["activation_error"],
                        row["activation_id"],
                    ),
                )
                updated = db.execute(
                    "UPDATE durable_worker_tasks SET status=?,revision=revision+1,updated_at=? "
                    "WHERE task_id=? AND parent_session_id=? AND status='in_progress'",
                    (target, _now(), row["task_id"], row["parent_session_id"]),
                ).rowcount
                recovered += int(updated == 1)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return recovered

    def recover_failed_task(
        self,
        parent: str,
        task_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        revision = self._expected_revision(expected_revision)
        db = self.store._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            task = self.store._owned_task(db, parent, task_id)
            if task["revision"] != revision:
                raise DurableWorkerConflictError(
                    f"task revision changed (expected {revision}, actual {task['revision']})"
                )
            if task["status"] != "failed":
                raise DurableWorkerConflictError("task recovery requires failed state")
            run = db.execute(
                "SELECT * FROM durable_worker_task_runs WHERE task_id=? "
                "ORDER BY created_at DESC,activation_id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if run is None:
                raise DurableWorkerConflictError("failed task has no orchestration run")
            activation = db.execute(
                "SELECT * FROM durable_worker_activations WHERE activation_id=?",
                (run["activation_id"],),
            ).fetchone()
            if activation is None or activation["state"] in {
                "STARTING",
                "RUNNING",
                "CANCEL_REQUESTED",
                "SUCCEEDED",
            }:
                raise DurableWorkerConflictError(
                    "failed task does not reference a retryable terminal activation"
                )
            worker = self.store._owned_worker(db, parent, run["worker_id"])
            message = db.execute(
                "SELECT * FROM durable_worker_messages "
                "WHERE message_id=? AND worker_id=? AND direction='parent'",
                (run["message_id"], run["worker_id"]),
            ).fetchone()
            if message is None:
                raise DurableWorkerConflictError("failed task message is missing")

            if worker["status"] == "FAILED" and message["state"] == "FAILED":
                now = _now()
                db.execute(
                    "UPDATE durable_worker_messages SET state='PENDING',updated_at=? "
                    "WHERE message_id=? AND worker_id=? AND state='FAILED'",
                    (now, run["message_id"], run["worker_id"]),
                )
                db.execute(
                    "UPDATE durable_workers SET status='DORMANT',updated_at=?,"
                    "revision=revision+1 WHERE worker_id=? AND parent_session_id=? "
                    "AND status='FAILED'",
                    (now, run["worker_id"], parent),
                )
            elif not (
                worker["status"] == "DORMANT" and message["state"] == "PENDING"
            ):
                raise DurableWorkerConflictError(
                    "failed task worker/message are not in a recoverable state"
                )

            now = _now()
            db.execute(
                "UPDATE durable_worker_tasks SET status='pending',revision=revision+1,"
                "updated_at=? WHERE task_id=? AND parent_session_id=? AND status='failed'",
                (now, task_id, parent),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        return {
            "status": "RECOVERY_READY",
            "task": self.store.get_task(parent, task_id),
            "worker": self.store.get_worker(parent, run["worker_id"]),
            "message_id": str(run["message_id"]),
            "previous_activation_id": str(run["activation_id"]),
        }

    def reserve_ready_task(
        self,
        parent: str,
        task_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Reserve READY task, reusing only its own requeued durable message."""
        revision = self._expected_revision(expected_revision)
        db = self.store._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            task = self.store._owned_task(db, parent, task_id)
            if task["revision"] != revision:
                raise DurableWorkerConflictError(
                    f"task revision changed (expected {revision}, actual {task['revision']})"
                )
            if task["status"] != "pending":
                raise DurableWorkerConflictError("task dispatch requires pending state")
            worker_id = str(task["worker_id"] or "").strip()
            if not worker_id:
                raise DurableWorkerConflictError("task dispatch requires an assigned worker")

            blockers = db.execute(
                "SELECT d.blocked_by_task_id,t.status "
                "FROM durable_worker_task_dependencies d "
                "JOIN durable_worker_tasks t ON t.task_id=d.blocked_by_task_id "
                "WHERE d.task_id=?",
                (task_id,),
            ).fetchall()
            incomplete = [
                row["blocked_by_task_id"]
                for row in blockers
                if row["status"] != "completed"
            ]
            if incomplete:
                raise DurableWorkerConflictError(
                    "task is blocked by incomplete dependencies: " + ", ".join(incomplete)
                )

            worker = self.store._owned_worker(db, parent, worker_id)
            if worker["status"] != "DORMANT":
                raise DurableWorkerConflictError(
                    f"assigned worker is not dispatchable from {worker['status']}"
                )

            pending_rows = db.execute(
                "SELECT * FROM durable_worker_messages "
                "WHERE worker_id=? AND direction='parent' AND state='PENDING' "
                "ORDER BY created_at,message_id",
                (worker_id,),
            ).fetchall()
            latest_run = db.execute(
                "SELECT * FROM durable_worker_task_runs WHERE task_id=? "
                "ORDER BY created_at DESC,activation_id DESC LIMIT 1",
                (task_id,),
            ).fetchone()

            now = _now()
            if pending_rows:
                if (
                    len(pending_rows) != 1
                    or latest_run is None
                    or pending_rows[0]["message_id"] != latest_run["message_id"]
                    or latest_run["worker_id"] != worker_id
                ):
                    raise DurableWorkerConflictError(
                        "assigned worker has unrelated pending durable inbox messages"
                    )
                message = pending_rows[0]
                message_id = str(message["message_id"])
                content = str(message["content"])
                updated = db.execute(
                    "UPDATE durable_worker_messages SET state='PROCESSING',updated_at=? "
                    "WHERE message_id=? AND worker_id=? AND state='PENDING'",
                    (now, message_id, worker_id),
                ).rowcount
                if updated != 1:
                    raise DurableWorkerConflictError(
                        "task durable message changed while redispatching"
                    )
            else:
                message_id = _id("dwm")
                description = str(task["description"] or "").strip()
                content = (
                    f"Hermes durable task {task_id}\n"
                    f"Subject: {task['subject']}\n\n"
                    + (description + "\n\n" if description else "")
                    + "Complete this assigned task and return a concise result summary."
                )
                db.execute(
                    "INSERT INTO durable_worker_messages "
                    "(message_id,worker_id,direction,content,state,created_at,updated_at) "
                    "VALUES(?,?,'parent',?,'PROCESSING',?,?)",
                    (message_id, worker_id, content, now, now),
                )

            activation_id = _id("dwa")
            owner_pid = os.getpid()
            owner_started_at = _process_start_time(owner_pid)
            db.execute(
                "INSERT INTO durable_worker_activations "
                "(activation_id,worker_id,message_id,state,started_at,owner_pid,owner_started_at) "
                "VALUES(?,?,?,'STARTING',?,?,?)",
                (
                    activation_id,
                    worker_id,
                    message_id,
                    now,
                    owner_pid,
                    owner_started_at,
                ),
            )
            db.execute(
                "UPDATE durable_workers SET status='RUNNING',updated_at=?,"
                "revision=revision+1,last_activation_id=? WHERE worker_id=?",
                (now, activation_id, worker_id),
            )
            db.execute(
                "UPDATE durable_worker_tasks SET status='in_progress',"
                "revision=revision+1,updated_at=? "
                "WHERE task_id=? AND parent_session_id=?",
                (now, task_id, parent),
            )
            db.execute(
                "INSERT INTO durable_worker_task_runs "
                "(activation_id,task_id,message_id,worker_id,state,created_at) "
                "VALUES(?,?,?,?, 'STARTING', ?)",
                (activation_id, task_id, message_id, worker_id, now),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        return {
            "status": "RESERVED",
            "task_id": task_id,
            "worker_id": worker_id,
            "activation_id": activation_id,
            "message": {
                "message_id": message_id,
                "worker_id": worker_id,
                "direction": "parent",
                "content": content,
                "state": "PROCESSING",
                "created_at": now,
                "updated_at": now,
            },
            "task": self.store.get_task(parent, task_id),
            "reused_message": bool(pending_rows),
        }


__all__ = ["RecoverableDurableTaskOrchestrator"]
