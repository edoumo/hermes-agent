"""H5 durable task orchestration for Hermes Durable Workers.

H1/H4 remain the source of truth for workers, messages and activations.  H5
adds an orchestration layer that can safely edit a pending DAG, atomically
reserve a READY task into a real Durable Worker activation, and reconcile the
worker result back into task state.

Live lifecycle handles are never persisted.  The only H5 persistence is a
small audit table linking task -> durable message -> activation.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

from agent.durable_workers import (
    DurableWorkerConflictError,
    DurableWorkerError,
    DurableWorkerStore,
    _id,
    _now,
    _process_start_time,
)


class DurableTaskOrchestrator:
    """Transactional H5 task-DAG and dispatch operations."""

    def __init__(self, store: DurableWorkerStore):
        self.store = store
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.store._db() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS durable_worker_task_runs(
                  activation_id TEXT PRIMARY KEY
                    REFERENCES durable_worker_activations(activation_id) ON DELETE CASCADE,
                  task_id TEXT NOT NULL
                    REFERENCES durable_worker_tasks(task_id) ON DELETE CASCADE,
                  message_id TEXT NOT NULL
                    REFERENCES durable_worker_messages(message_id) ON DELETE CASCADE,
                  worker_id TEXT NOT NULL
                    REFERENCES durable_workers(worker_id) ON DELETE CASCADE,
                  state TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  completed_at REAL,
                  summary TEXT,
                  error TEXT
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_dwtr_task "
                "ON durable_worker_task_runs(task_id, created_at DESC)"
            )

    @staticmethod
    def _expected_revision(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise DurableWorkerError("expected_revision must be an integer")
        return value

    def edit_task(
        self,
        parent: str,
        task_id: str,
        *,
        expected_revision: int,
        subject: Optional[str] = None,
        description: Optional[str] = None,
        worker_id: Optional[str] = None,
        worker_id_present: bool = False,
    ) -> dict[str, Any]:
        revision = self._expected_revision(expected_revision)
        if subject is None and description is None and not worker_id_present:
            raise DurableWorkerError("task edit requires at least one editable field")
        if subject is not None:
            subject = str(subject).strip()
            if not subject or len(subject) > 300:
                raise DurableWorkerError("task subject must contain 1..300 characters")
        if description is not None:
            description = str(description)
            if len(description) > 16000:
                raise DurableWorkerError("task description must contain at most 16000 characters")
        if worker_id_present:
            worker_id = str(worker_id or "").strip() or None

        db = self.store._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            task = self.store._owned_task(db, parent, task_id)
            if task["revision"] != revision:
                raise DurableWorkerConflictError(
                    f"task revision changed (expected {revision}, actual {task['revision']})"
                )
            if task["status"] != "pending":
                raise DurableWorkerConflictError(
                    "only pending tasks can be edited or reassigned"
                )
            if worker_id_present and worker_id is not None:
                self.store._owned_worker(db, parent, worker_id)

            assignments: list[str] = []
            args: list[Any] = []
            if subject is not None:
                assignments.append("subject=?")
                args.append(subject)
            if description is not None:
                assignments.append("description=?")
                args.append(description)
            if worker_id_present:
                assignments.append("worker_id=?")
                args.append(worker_id)
            assignments.extend(["revision=revision+1", "updated_at=?"])
            args.extend([_now(), task_id, parent])
            updated = db.execute(
                "UPDATE durable_worker_tasks SET "
                + ",".join(assignments)
                + " WHERE task_id=? AND parent_session_id=?",
                tuple(args),
            ).rowcount
            if updated != 1:
                raise DurableWorkerConflictError("task changed during edit")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return self.store.get_task(parent, task_id)

    def add_dependency(
        self,
        parent: str,
        task_id: str,
        blocked_by: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        revision = self._expected_revision(expected_revision)
        if task_id == blocked_by:
            raise DurableWorkerConflictError("task dependency cycle")
        db = self.store._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            task = self.store._owned_task(db, parent, task_id)
            self.store._owned_task(db, parent, blocked_by)
            if task["revision"] != revision:
                raise DurableWorkerConflictError(
                    f"task revision changed (expected {revision}, actual {task['revision']})"
                )
            if task["status"] != "pending":
                raise DurableWorkerConflictError(
                    "dependencies can only change while task is pending"
                )
            exists = db.execute(
                "SELECT 1 FROM durable_worker_task_dependencies "
                "WHERE task_id=? AND blocked_by_task_id=?",
                (task_id, blocked_by),
            ).fetchone()
            if exists:
                db.commit()
                out = self.store.get_task(parent, task_id)
                out["changed"] = False
                return out

            graph: dict[str, set[str]] = {}
            for source, dependency in db.execute(
                "SELECT d.task_id,d.blocked_by_task_id "
                "FROM durable_worker_task_dependencies d "
                "JOIN durable_worker_tasks t ON t.task_id=d.task_id "
                "WHERE t.parent_session_id=?",
                (parent,),
            ):
                graph.setdefault(source, set()).add(dependency)
            graph.setdefault(task_id, set()).add(blocked_by)

            def reaches(node: str, target: str, seen: set[str]) -> bool:
                if node == target:
                    return True
                if node in seen:
                    return False
                return any(
                    reaches(next_node, target, seen | {node})
                    for next_node in graph.get(node, ())
                )

            if reaches(blocked_by, task_id, set()):
                raise DurableWorkerConflictError("task dependency cycle")
            db.execute(
                "INSERT INTO durable_worker_task_dependencies VALUES(?,?)",
                (task_id, blocked_by),
            )
            db.execute(
                "UPDATE durable_worker_tasks SET revision=revision+1,updated_at=? "
                "WHERE task_id=? AND parent_session_id=?",
                (_now(), task_id, parent),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        out = self.store.get_task(parent, task_id)
        out["changed"] = True
        return out

    def remove_dependency(
        self,
        parent: str,
        task_id: str,
        blocked_by: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        revision = self._expected_revision(expected_revision)
        db = self.store._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            task = self.store._owned_task(db, parent, task_id)
            self.store._owned_task(db, parent, blocked_by)
            if task["revision"] != revision:
                raise DurableWorkerConflictError(
                    f"task revision changed (expected {revision}, actual {task['revision']})"
                )
            if task["status"] != "pending":
                raise DurableWorkerConflictError(
                    "dependencies can only change while task is pending"
                )
            removed = db.execute(
                "DELETE FROM durable_worker_task_dependencies "
                "WHERE task_id=? AND blocked_by_task_id=?",
                (task_id, blocked_by),
            ).rowcount
            if removed:
                db.execute(
                    "UPDATE durable_worker_tasks SET revision=revision+1,updated_at=? "
                    "WHERE task_id=? AND parent_session_id=?",
                    (_now(), task_id, parent),
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        out = self.store.get_task(parent, task_id)
        out["changed"] = bool(removed)
        return out

    def reserve_ready_task(
        self,
        parent: str,
        task_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Atomically turn one READY task into a real worker activation."""
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
            pending = db.execute(
                "SELECT COUNT(*) FROM durable_worker_messages "
                "WHERE worker_id=? AND direction='parent' AND state='PENDING'",
                (worker_id,),
            ).fetchone()[0]
            if pending:
                raise DurableWorkerConflictError(
                    "assigned worker has pending durable inbox messages; run them before dispatching a task"
                )

            now = _now()
            message_id = _id("dwm")
            activation_id = _id("dwa")
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
            owner_pid = __import__("os").getpid()
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
        }

    def reconcile_result(
        self,
        parent: str,
        task_id: str,
        activation_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Project a terminal worker result back into its task audit state."""
        state = str(result.get("status") or "UNKNOWN")
        summary = result.get("summary")
        error = result.get("error")
        if state == "CANCEL_REQUESTED":
            target_status: Optional[str] = None
            completed_at = None
        elif state == "SUCCEEDED":
            target_status = "completed"
            completed_at = _now()
        elif state == "CANCELLED" and bool(result.get("retryable")):
            target_status = "pending"
            completed_at = _now()
        else:
            target_status = "failed"
            completed_at = _now()

        db = self.store._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            task = self.store._owned_task(db, parent, task_id)
            run = db.execute(
                "SELECT * FROM durable_worker_task_runs "
                "WHERE activation_id=? AND task_id=?",
                (activation_id, task_id),
            ).fetchone()
            if run is None:
                raise DurableWorkerConflictError("task activation audit row is missing")
            latest = db.execute(
                "SELECT activation_id FROM durable_worker_task_runs "
                "WHERE task_id=? ORDER BY created_at DESC,activation_id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            db.execute(
                "UPDATE durable_worker_task_runs SET state=?,completed_at=?,summary=?,error=? "
                "WHERE activation_id=?",
                (
                    state,
                    completed_at,
                    str(summary)[:32000] if summary is not None else None,
                    str(error)[:32000] if error is not None else None,
                    activation_id,
                ),
            )
            if (
                target_status is not None
                and latest is not None
                and latest["activation_id"] == activation_id
                and task["status"] == "in_progress"
            ):
                db.execute(
                    "UPDATE durable_worker_tasks SET status=?,revision=revision+1,updated_at=? "
                    "WHERE task_id=? AND parent_session_id=? AND status='in_progress'",
                    (target_status, _now(), task_id, parent),
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return self.store.get_task(parent, task_id)

    def reconcile_exception(
        self,
        parent: str,
        task_id: str,
        activation_id: str,
        error: Exception,
    ) -> dict[str, Any]:
        """Repair only launch failures; other exceptional states stay fail-closed."""
        db = self.store._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            task = self.store._owned_task(db, parent, task_id)
            activation = db.execute(
                "SELECT state FROM durable_worker_activations WHERE activation_id=?",
                (activation_id,),
            ).fetchone()
            state = str(activation["state"] if activation else "UNKNOWN")
            db.execute(
                "UPDATE durable_worker_task_runs SET state=?,completed_at=?,error=? "
                "WHERE activation_id=? AND task_id=?",
                (state, _now(), str(error)[:32000], activation_id, task_id),
            )
            if state == "FAILED_TO_START" and task["status"] == "in_progress":
                db.execute(
                    "UPDATE durable_worker_tasks SET status='pending',"
                    "revision=revision+1,updated_at=? "
                    "WHERE task_id=? AND parent_session_id=?",
                    (_now(), task_id, parent),
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return self.store.get_task(parent, task_id)


class DurableTaskGraphProjection:
    """Read-only H5 graph projection; never initializes or mutates the store."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise DurableWorkerError("Durable worker store is not initialized.")
        uri = f"file:{self.db_path.resolve()}?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=1.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db

    @staticmethod
    def _ready(db: sqlite3.Connection, task: sqlite3.Row, blocked_by: list[str]) -> bool:
        if task["status"] != "pending":
            return False
        if not blocked_by:
            return True
        placeholders = ",".join("?" for _ in blocked_by)
        states = {
            row["task_id"]: row["status"]
            for row in db.execute(
                f"SELECT task_id,status FROM durable_worker_tasks "
                f"WHERE task_id IN({placeholders})",
                tuple(blocked_by),
            )
        }
        return all(states.get(task_id) == "completed" for task_id in blocked_by)

    def get_task(self, parent: str, task_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM durable_worker_tasks WHERE parent_session_id=? AND task_id=?",
                (parent, task_id),
            ).fetchone()
            if row is None:
                from agent.durable_workers_api import NotFoundError

                raise NotFoundError("Durable task not found for this session.")
            blocked_by = [
                item[0]
                for item in db.execute(
                    "SELECT blocked_by_task_id FROM durable_worker_task_dependencies "
                    "WHERE task_id=? ORDER BY blocked_by_task_id",
                    (task_id,),
                )
            ]
            out = dict(row)
            out["blocked_by"] = blocked_by
            out["ready"] = self._ready(db, row, blocked_by)
            return out

    def graph(self, parent: str, *, limit: int = 100) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 100:
            raise DurableWorkerError("graph limit must be an integer between 1 and 100")
        with self._connect() as db:
            total = int(
                db.execute(
                    "SELECT COUNT(*) FROM durable_worker_tasks WHERE parent_session_id=?",
                    (parent,),
                ).fetchone()[0]
            )
            rows = db.execute(
                "SELECT * FROM durable_worker_tasks WHERE parent_session_id=? "
                "ORDER BY created_at,task_id LIMIT ?",
                (parent, limit),
            ).fetchall()
            included = {row["task_id"] for row in rows}
            tasks: list[dict[str, Any]] = []
            edges: list[dict[str, str]] = []
            run_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='durable_worker_task_runs'"
            ).fetchone()
            for row in rows:
                task_id = str(row["task_id"])
                blockers = [
                    item[0]
                    for item in db.execute(
                        "SELECT blocked_by_task_id FROM durable_worker_task_dependencies "
                        "WHERE task_id=? ORDER BY blocked_by_task_id",
                        (task_id,),
                    )
                ]
                dependents = [
                    item[0]
                    for item in db.execute(
                        "SELECT task_id FROM durable_worker_task_dependencies "
                        "WHERE blocked_by_task_id=? ORDER BY task_id",
                        (task_id,),
                    )
                ]
                item = dict(row)
                item["blocked_by"] = blockers
                item["dependents"] = dependents
                item["ready"] = self._ready(db, row, blockers)
                item["last_run"] = None
                if run_table:
                    run = db.execute(
                        "SELECT activation_id,message_id,worker_id,state,created_at,completed_at,summary,error "
                        "FROM durable_worker_task_runs WHERE task_id=? "
                        "ORDER BY created_at DESC,activation_id DESC LIMIT 1",
                        (task_id,),
                    ).fetchone()
                    if run is not None:
                        item["last_run"] = dict(run)
                tasks.append(item)
                for blocker in blockers:
                    if blocker in included:
                        edges.append({"from": str(blocker), "to": task_id})

        counts = {
            "total": total,
            "pending": 0,
            "ready": 0,
            "blocked": 0,
            "in_progress": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
        }
        for task in tasks:
            status = str(task.get("status") or "")
            if status in counts:
                counts[status] += 1
            if status == "pending":
                if task.get("ready"):
                    counts["ready"] += 1
                else:
                    counts["blocked"] += 1
        return {
            "tasks": tasks,
            "edges": edges,
            "counts": counts,
            "truncated": total > len(tasks),
        }


__all__ = ["DurableTaskGraphProjection", "DurableTaskOrchestrator"]
