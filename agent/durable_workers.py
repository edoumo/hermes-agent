"""Experimental durable worker primitives for Hermes H1.

A durable worker is persistent identity plus transcript. Each turn launches a
fresh existing Hermes subagent activation. No AIAgent, credential, callback,
thread, or socket is serialized. The module is UI agnostic and the bundled
``durable-workers`` plugin is opt in.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

WORKER_STATES = {"DORMANT", "RUNNING", "FAILED", "DISABLED"}
MESSAGE_STATES = {"PENDING", "PROCESSING", "CONSUMED", "FAILED", "COMPLETE"}
TASK_STATES = {"pending", "in_progress", "completed", "failed", "cancelled"}


class DurableWorkerError(ValueError):
    """Base error for experimental durable worker operations."""


class DurableWorkerAuthorizationError(DurableWorkerError):
    """The active parent does not own the addressed worker or task."""


class DurableWorkerConflictError(DurableWorkerError):
    """A durable state transition conflicts with the current persisted state."""


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _now() -> float:
    return time.time()


def _process_start_time(pid: int) -> Optional[int]:
    """Return Hermes' cross-platform process birth marker when available."""
    try:
        from gateway.status import get_process_start_time

        started = get_process_start_time(pid)
        return int(started) if started is not None else None
    except Exception:
        return None


def _owner_alive(pid: Optional[int], started_at: Optional[int]) -> bool:
    """Check PID plus birth marker so PID reuse cannot preserve stale work."""
    if not pid or pid <= 0:
        return False
    if pid == os.getpid():
        current = _process_start_time(pid)
        return started_at is None or current is None or current == int(started_at)
    try:
        from gateway.status import _pid_exists, get_process_start_time

        if not _pid_exists(int(pid)):
            return False
        if started_at is None:
            return True
        current = get_process_start_time(int(pid))
        return current is not None and int(current) == int(started_at)
    except Exception:
        # Import-light fallback for isolated tests. Hermes production carries
        # gateway.status and therefore uses the stronger cross-platform check.
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return started_at is None


def _default_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "durable-workers.db"


class DurableWorkerStore:
    """SQLite-backed durable identity, inbox, activation and task state."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else _default_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self.recover_abandoned_activations()

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=1.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        return db

    def _init_schema(self) -> None:
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS durable_workers(
                  worker_id TEXT PRIMARY KEY,
                  parent_session_id TEXT NOT NULL,
                  label TEXT NOT NULL,
                  status TEXT NOT NULL,
                  role TEXT NOT NULL,
                  model TEXT,
                  toolsets_json TEXT,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  revision INTEGER NOT NULL DEFAULT 1,
                  last_activation_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_dw_parent
                  ON durable_workers(parent_session_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS durable_worker_messages(
                  message_id TEXT PRIMARY KEY,
                  worker_id TEXT NOT NULL REFERENCES durable_workers(worker_id) ON DELETE CASCADE,
                  direction TEXT NOT NULL,
                  content TEXT NOT NULL,
                  state TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_dwm_pending
                  ON durable_worker_messages(worker_id, state, created_at, message_id);

                CREATE TABLE IF NOT EXISTS durable_worker_activations(
                  activation_id TEXT PRIMARY KEY,
                  worker_id TEXT NOT NULL REFERENCES durable_workers(worker_id) ON DELETE CASCADE,
                  message_id TEXT REFERENCES durable_worker_messages(message_id) ON DELETE SET NULL,
                  subagent_id TEXT,
                  state TEXT NOT NULL,
                  started_at REAL NOT NULL,
                  completed_at REAL,
                  summary TEXT,
                  error TEXT,
                  owner_pid INTEGER,
                  owner_started_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_dwa_worker
                  ON durable_worker_activations(worker_id, started_at DESC);

                CREATE TABLE IF NOT EXISTS durable_worker_tasks(
                  task_id TEXT PRIMARY KEY,
                  parent_session_id TEXT NOT NULL,
                  worker_id TEXT REFERENCES durable_workers(worker_id) ON DELETE SET NULL,
                  subject TEXT NOT NULL,
                  description TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL,
                  revision INTEGER NOT NULL DEFAULT 1,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS durable_worker_task_dependencies(
                  task_id TEXT NOT NULL REFERENCES durable_worker_tasks(task_id) ON DELETE CASCADE,
                  blocked_by_task_id TEXT NOT NULL REFERENCES durable_worker_tasks(task_id) ON DELETE CASCADE,
                  PRIMARY KEY(task_id, blocked_by_task_id)
                );
                """
            )
            columns = {
                row[1]
                for row in db.execute(
                    "PRAGMA table_info(durable_worker_activations)"
                ).fetchall()
            }
            if "owner_started_at" not in columns:
                db.execute(
                    "ALTER TABLE durable_worker_activations "
                    "ADD COLUMN owner_started_at INTEGER"
                )

    def _owned_worker(self, db: sqlite3.Connection, parent: str, worker_id: str):
        row = db.execute(
            "SELECT * FROM durable_workers "
            "WHERE worker_id=? AND parent_session_id=?",
            (worker_id, parent),
        ).fetchone()
        if row is None:
            raise DurableWorkerAuthorizationError(
                "Durable worker not found for the active parent session."
            )
        return row

    @staticmethod
    def _worker(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["toolsets"] = json.loads(out.pop("toolsets_json") or "[]")
        return out

    def create_worker(
        self,
        parent: str,
        *,
        label: str,
        role: str = "leaf",
        model: Optional[str] = None,
        toolsets: Optional[Iterable[str]] = None,
        max_workers: int = 64,
    ) -> dict[str, Any]:
        parent = str(parent or "").strip()
        label = str(label).strip()
        if not parent:
            raise DurableWorkerAuthorizationError("parent session id is required")
        if not label or len(label) > 160:
            raise DurableWorkerError("label must contain 1..160 characters")
        if role not in {"leaf", "orchestrator"}:
            raise DurableWorkerError("role must be leaf or orchestrator")
        normalized_toolsets = [str(value) for value in (toolsets or [])]
        now, worker_id = _now(), _id("dw")
        with self._db() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM durable_workers WHERE parent_session_id=?",
                (parent,),
            ).fetchone()[0]
            if count >= max_workers:
                raise DurableWorkerConflictError(
                    "durable worker limit reached for parent session"
                )
            db.execute(
                "INSERT INTO durable_workers VALUES(?,?,?,?,?,?,?,?,?,1,NULL)",
                (
                    worker_id,
                    parent,
                    label,
                    "DORMANT",
                    role,
                    model,
                    json.dumps(normalized_toolsets),
                    now,
                    now,
                ),
            )
        return self.get_worker(parent, worker_id)

    def get_worker(self, parent: str, worker_id: str) -> dict[str, Any]:
        with self._db() as db:
            return self._worker(self._owned_worker(db, parent, worker_id))

    def list_workers(self, parent: str) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM durable_workers WHERE parent_session_id=? "
                "ORDER BY updated_at DESC",
                (parent,),
            )
            return [self._worker(row) for row in rows]

    def enqueue_message(
        self,
        parent: str,
        worker_id: str,
        content: str,
        *,
        message_id: Optional[str] = None,
    ) -> dict[str, Any]:
        content = str(content or "").strip()
        if not content or len(content) > 32000:
            raise DurableWorkerError("message must contain 1..32000 characters")
        message_id, now = str(message_id or _id("dwm")), _now()
        with self._db() as db:
            self._owned_worker(db, parent, worker_id)
            old = db.execute(
                "SELECT * FROM durable_worker_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if old:
                if (
                    old["worker_id"] != worker_id
                    or old["direction"] != "parent"
                    or old["content"] != content
                ):
                    raise DurableWorkerConflictError(
                        "message_id already exists with different durable content"
                    )
                return {**dict(old), "created": False}
            db.execute(
                "INSERT INTO durable_worker_messages "
                "VALUES(?,?,'parent',?,'PENDING',?,?)",
                (message_id, worker_id, content, now, now),
            )
        return {
            "message_id": message_id,
            "worker_id": worker_id,
            "direction": "parent",
            "content": content,
            "state": "PENDING",
            "created_at": now,
            "updated_at": now,
            "created": True,
        }

    def reserve_next_activation(
        self, parent: str, worker_id: str
    ) -> dict[str, Any]:
        """Atomically reserve one worker, one pending message and one activation.

        The worker status is the cross-process mutex. Two Hermes processes may
        race on the same durable worker, but only one transaction can move it
        DORMANT -> RUNNING and claim a message.
        """
        db = self._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            worker = self._owned_worker(db, parent, worker_id)
            if worker["status"] == "RUNNING":
                db.commit()
                return {"worker_id": worker_id, "status": "BUSY"}
            if worker["status"] == "DISABLED":
                db.commit()
                return {"worker_id": worker_id, "status": "DISABLED"}
            if worker["status"] == "FAILED":
                db.commit()
                return {"worker_id": worker_id, "status": "FAILED_NEEDS_REVIEW"}

            message = db.execute(
                "SELECT * FROM durable_worker_messages "
                "WHERE worker_id=? AND direction='parent' AND state='PENDING' "
                "ORDER BY created_at, message_id LIMIT 1",
                (worker_id,),
            ).fetchone()
            if message is None:
                db.commit()
                return {"worker_id": worker_id, "status": "NO_PENDING_MESSAGE"}

            now = _now()
            activation_id = _id("dwa")
            owner_pid = os.getpid()
            owner_started_at = _process_start_time(owner_pid)
            updated = db.execute(
                "UPDATE durable_worker_messages "
                "SET state='PROCESSING', updated_at=? "
                "WHERE message_id=? AND state='PENDING'",
                (now, message["message_id"]),
            ).rowcount
            if updated != 1:
                db.rollback()
                return {"worker_id": worker_id, "status": "RACE_RETRY"}
            db.execute(
                "INSERT INTO durable_worker_activations "
                "(activation_id,worker_id,message_id,state,started_at,owner_pid,owner_started_at) "
                "VALUES(?,?,?,'STARTING',?,?,?)",
                (
                    activation_id,
                    worker_id,
                    message["message_id"],
                    now,
                    owner_pid,
                    owner_started_at,
                ),
            )
            db.execute(
                "UPDATE durable_workers SET status='RUNNING', updated_at=?, "
                "revision=revision+1, last_activation_id=? WHERE worker_id=?",
                (now, activation_id, worker_id),
            )
            db.commit()
            claimed = dict(message)
            claimed["state"] = "PROCESSING"
            return {
                "worker_id": worker_id,
                "status": "RESERVED",
                "activation_id": activation_id,
                "message": claimed,
            }
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def bind_activation(self, activation_id: str, subagent_id: str) -> None:
        with self._db() as db:
            updated = db.execute(
                "UPDATE durable_worker_activations "
                "SET subagent_id=?, state='RUNNING' "
                "WHERE activation_id=? AND state='STARTING'",
                (subagent_id, activation_id),
            ).rowcount
            if updated != 1:
                raise DurableWorkerConflictError(
                    "activation is no longer in STARTING state"
                )

    def finish_activation(
        self,
        parent: str,
        worker_id: str,
        activation_id: str,
        message_id: str,
        *,
        state: str,
        summary: Optional[str] = None,
        error: Optional[str] = None,
        message_state: str,
        worker_state: str,
    ) -> Optional[dict[str, Any]]:
        """Atomically finalize activation, inbox item, report and worker state."""
        if message_state not in MESSAGE_STATES:
            raise DurableWorkerError(f"unsupported message state: {message_state}")
        if worker_state not in WORKER_STATES:
            raise DurableWorkerError(f"unsupported worker state: {worker_state}")
        now = _now()
        report = None
        db = self._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            self._owned_worker(db, parent, worker_id)
            activation = db.execute(
                "SELECT * FROM durable_worker_activations "
                "WHERE activation_id=? AND worker_id=?",
                (activation_id, worker_id),
            ).fetchone()
            if activation is None:
                raise DurableWorkerConflictError("activation not found for worker")
            if activation["message_id"] != message_id:
                raise DurableWorkerConflictError("activation message mismatch")
            db.execute(
                "UPDATE durable_worker_messages SET state=?, updated_at=? "
                "WHERE message_id=? AND worker_id=?",
                (message_state, now, message_id, worker_id),
            )
            if state == "SUCCEEDED":
                report_text = str(summary or "(completed without summary)")[:32000]
                report_id = _id("dwm")
                db.execute(
                    "INSERT INTO durable_worker_messages "
                    "VALUES(?,?,'worker',?,'COMPLETE',?,?)",
                    (report_id, worker_id, report_text, now, now),
                )
                report = {
                    "message_id": report_id,
                    "worker_id": worker_id,
                    "content": report_text,
                }
            db.execute(
                "UPDATE durable_worker_activations "
                "SET state=?, completed_at=?, summary=?, error=? "
                "WHERE activation_id=?",
                (state, now, summary, error, activation_id),
            )
            db.execute(
                "UPDATE durable_workers SET status=?, updated_at=?, "
                "revision=revision+1, last_activation_id=? WHERE worker_id=?",
                (worker_state, now, activation_id, worker_id),
            )
            db.commit()
            return report
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def mark_cancel_requested(
        self, parent: str, worker_id: str, activation_id: str
    ) -> None:
        """Keep the worker locked while a timed-out live child unwinds."""
        with self._db() as db:
            self._owned_worker(db, parent, worker_id)
            db.execute(
                "UPDATE durable_worker_activations SET state='CANCEL_REQUESTED' "
                "WHERE activation_id=? AND worker_id=? "
                "AND state IN('STARTING','RUNNING')",
                (activation_id, worker_id),
            )

    def list_activations(self, parent: str, worker_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            self._owned_worker(db, parent, worker_id)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM durable_worker_activations "
                    "WHERE worker_id=? ORDER BY started_at",
                    (worker_id,),
                )
            ]

    def list_messages(
        self, parent: str, worker_id: str, *, direction: Optional[str] = None
    ) -> list[dict[str, Any]]:
        with self._db() as db:
            self._owned_worker(db, parent, worker_id)
            sql = "SELECT * FROM durable_worker_messages WHERE worker_id=?"
            args: list[Any] = [worker_id]
            if direction:
                sql += " AND direction=?"
                args.append(direction)
            sql += " ORDER BY created_at, message_id"
            return [dict(row) for row in db.execute(sql, args)]

    def render_context(
        self,
        parent: str,
        worker_id: str,
        *,
        exclude_message_id: Optional[str] = None,
        max_chars: int = 24000,
    ) -> str:
        worker = self.get_worker(parent, worker_id)
        with self._db() as db:
            rows = db.execute(
                "SELECT message_id,direction,content "
                "FROM durable_worker_messages "
                "WHERE worker_id=? AND state IN('CONSUMED','COMPLETE') "
                "ORDER BY created_at,message_id",
                (worker_id,),
            ).fetchall()
        header = [
            f"Durable worker: {worker['label']} ({worker_id})",
            "This is a new runtime activation of the same durable worker identity.",
            "Use the prior durable transcript as context; do not claim the Python process itself persisted.",
            "",
            "Prior durable transcript:",
        ]
        transcript = []
        for row in rows:
            if row["message_id"] == exclude_message_id:
                continue
            speaker = "PARENT" if row["direction"] == "parent" else "WORKER"
            transcript.append(f"{speaker}: {row['content']}")
        text = "\n".join(header + transcript)
        if len(text) <= max_chars:
            return text
        prefix = "\n".join(header) + "\n[older transcript truncated]\n"
        remaining = max(0, max_chars - len(prefix))
        return prefix + "\n".join(transcript)[-remaining:]

    def recover_abandoned_activations(self) -> int:
        recovered = 0
        with self._db() as db:
            rows = db.execute(
                "SELECT activation_id,worker_id,message_id,owner_pid,owner_started_at "
                "FROM durable_worker_activations "
                "WHERE state IN('STARTING','RUNNING','CANCEL_REQUESTED')"
            ).fetchall()
            for row in rows:
                if _owner_alive(row["owner_pid"], row["owner_started_at"]):
                    continue
                now = _now()
                recovered += 1
                db.execute(
                    "UPDATE durable_worker_activations "
                    "SET state='ABANDONED', completed_at=?, "
                    "error='owner process disappeared' WHERE activation_id=?",
                    (now, row["activation_id"]),
                )
                if row["message_id"]:
                    db.execute(
                        "UPDATE durable_worker_messages "
                        "SET state='PENDING', updated_at=? "
                        "WHERE message_id=? AND state='PROCESSING'",
                        (now, row["message_id"]),
                    )
                db.execute(
                    "UPDATE durable_workers SET status='DORMANT', updated_at=?, "
                    "revision=revision+1 WHERE worker_id=? AND status='RUNNING'",
                    (now, row["worker_id"]),
                )
        return recovered

    def _owned_task(self, db: sqlite3.Connection, parent: str, task_id: str):
        row = db.execute(
            "SELECT * FROM durable_worker_tasks "
            "WHERE task_id=? AND parent_session_id=?",
            (task_id, parent),
        ).fetchone()
        if row is None:
            raise DurableWorkerAuthorizationError(
                "Durable task not found for the active parent session."
            )
        return row

    def create_task(
        self,
        parent: str,
        *,
        subject: str,
        description: str = "",
        worker_id: Optional[str] = None,
    ) -> dict[str, Any]:
        subject = str(subject).strip()
        description = str(description or "")
        if not subject or len(subject) > 300 or len(description) > 16000:
            raise DurableWorkerError("invalid task subject or description")
        if worker_id:
            self.get_worker(parent, worker_id)
        task_id, now = _id("dwt"), _now()
        with self._db() as db:
            db.execute(
                "INSERT INTO durable_worker_tasks "
                "VALUES(?,?,?,?,?,'pending',1,?,?)",
                (task_id, parent, worker_id, subject, description, now, now),
            )
        return self.get_task(parent, task_id)

    def _task(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        blockers = db.execute(
            "SELECT blocked_by_task_id FROM durable_worker_task_dependencies "
            "WHERE task_id=? ORDER BY blocked_by_task_id",
            (out["task_id"],),
        ).fetchall()
        out["blocked_by"] = [item[0] for item in blockers]
        if out["status"] != "pending":
            out["ready"] = False
        elif not blockers:
            out["ready"] = True
        else:
            placeholders = ",".join("?" for _ in blockers)
            states = {
                item["task_id"]: item["status"]
                for item in db.execute(
                    f"SELECT task_id,status FROM durable_worker_tasks "
                    f"WHERE task_id IN({placeholders})",
                    tuple(item[0] for item in blockers),
                )
            }
            out["ready"] = all(
                states.get(item[0]) == "completed" for item in blockers
            )
        return out

    def get_task(self, parent: str, task_id: str) -> dict[str, Any]:
        with self._db() as db:
            return self._task(db, self._owned_task(db, parent, task_id))

    def list_tasks(self, parent: str) -> list[dict[str, Any]]:
        with self._db() as db:
            return [
                self._task(db, row)
                for row in db.execute(
                    "SELECT * FROM durable_worker_tasks "
                    "WHERE parent_session_id=? ORDER BY created_at,task_id",
                    (parent,),
                )
            ]

    def add_task_dependency(
        self, parent: str, task_id: str, blocked_by: str
    ) -> dict[str, Any]:
        if task_id == blocked_by:
            raise DurableWorkerConflictError("task dependency cycle")
        with self._db() as db:
            self._owned_task(db, parent, task_id)
            self._owned_task(db, parent, blocked_by)
            graph: dict[str, set[str]] = {}
            for source, dependency in db.execute(
                "SELECT task_id,blocked_by_task_id "
                "FROM durable_worker_task_dependencies "
                "JOIN durable_worker_tasks USING(task_id) "
                "WHERE parent_session_id=?",
                (parent,),
            ):
                graph.setdefault(source, set()).add(dependency)
            graph.setdefault(task_id, set()).add(blocked_by)

            def reaches(node: str, target: str, stack: set[str]) -> bool:
                if node == target:
                    return True
                if node in stack:
                    return False
                return any(
                    reaches(next_node, target, stack | {node})
                    for next_node in graph.get(node, ())
                )

            if reaches(blocked_by, task_id, set()):
                raise DurableWorkerConflictError("task dependency cycle")
            db.execute(
                "INSERT OR IGNORE INTO durable_worker_task_dependencies VALUES(?,?)",
                (task_id, blocked_by),
            )
        return self.get_task(parent, task_id)

    def update_task(
        self,
        parent: str,
        task_id: str,
        *,
        status: str,
        expected_revision: Optional[int] = None,
    ) -> dict[str, Any]:
        if status not in TASK_STATES:
            raise DurableWorkerError(f"unsupported task status: {status}")
        db = self._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = self._owned_task(db, parent, task_id)
            if expected_revision is not None and row["revision"] != int(
                expected_revision
            ):
                raise DurableWorkerConflictError(
                    f"task revision changed (expected {expected_revision}, "
                    f"actual {row['revision']})"
                )
            db.execute(
                "UPDATE durable_worker_tasks "
                "SET status=?,revision=revision+1,updated_at=? WHERE task_id=?",
                (status, _now(), task_id),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return self.get_task(parent, task_id)


class DurableWorkerService:
    """Durable identity layered on Hermes' existing subagent lifecycle."""

    def __init__(
        self,
        store: DurableWorkerStore,
        lifecycle: Any,
        parent_resolver: Callable[[], Any],
    ):
        self.store = store
        self.lifecycle = lifecycle
        self.parent_resolver = parent_resolver

    def parent_session_id(self) -> str:
        parent = self.parent_resolver()
        session_id = str(getattr(parent, "session_id", "") or "").strip()
        if not session_id:
            raise DurableWorkerAuthorizationError(
                "Durable workers require an active Hermes parent session."
            )
        return session_id

    def create_worker(self, **kwargs: Any) -> dict[str, Any]:
        return self.store.create_worker(self.parent_session_id(), **kwargs)

    def list_workers(self) -> list[dict[str, Any]]:
        return self.store.list_workers(self.parent_session_id())

    def get_worker(self, worker_id: str) -> dict[str, Any]:
        return self.store.get_worker(self.parent_session_id(), worker_id)

    def enqueue(
        self,
        worker_id: str,
        message: str,
        *,
        message_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return self.store.enqueue_message(
            self.parent_session_id(), worker_id, message, message_id=message_id
        )

    def send(
        self,
        worker_id: str,
        message: str,
        *,
        message_id: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        queued = self.enqueue(worker_id, message, message_id=message_id)
        return {
            "queued": queued,
            "activation": self.run_next(
                worker_id, timeout_seconds=timeout_seconds
            ),
        }

    def run_next(
        self, worker_id: str, *, timeout_seconds: Optional[float] = None
    ) -> dict[str, Any]:
        parent = self.parent_session_id()
        worker = self.store.get_worker(parent, worker_id)
        reserved = self.store.reserve_next_activation(parent, worker_id)
        if reserved["status"] != "RESERVED":
            return reserved

        message = reserved["message"]
        activation_id = reserved["activation_id"]
        context = self.store.render_context(
            parent, worker_id, exclude_message_id=message["message_id"]
        )
        try:
            from agent.subagent_lifecycle import SubagentLaunchRequest

            handle = self.lifecycle.launch(
                SubagentLaunchRequest(
                    goal=message["content"],
                    context=context,
                    role=worker["role"],
                    model=worker["model"],
                    allowed_toolsets=tuple(worker["toolsets"]) or None,
                    parent_session_id=parent,
                    correlation_id=activation_id,
                    metadata={
                        "durable_worker_id": worker_id,
                        "durable_activation_id": activation_id,
                    },
                    timeout_seconds=timeout_seconds,
                )
            )
            self.store.bind_activation(activation_id, handle.subagent_id)
            terminal = self.lifecycle.wait(handle, timeout_seconds=timeout_seconds)
            if not terminal.completed:
                try:
                    self.lifecycle.cancel(
                        handle, reason="durable worker activation timeout"
                    )
                finally:
                    # Fail closed: the activation still owns this worker until
                    # its process disappears and crash recovery can prove it is
                    # abandoned. Never launch overlapping work after a timeout.
                    self.store.mark_cancel_requested(
                        parent, worker_id, activation_id
                    )
                return {
                    "worker_id": worker_id,
                    "activation_id": activation_id,
                    "subagent_id": handle.subagent_id,
                    "status": "CANCEL_REQUESTED",
                }

            result = self.lifecycle.result(handle)
            state = getattr(terminal.state, "value", str(terminal.state))
            summary = getattr(result, "summary", None)
            error = getattr(result, "error_message", None)
            if state == "SUCCEEDED" and getattr(result, "ready", False):
                text = str(summary or "(completed without summary)")[:32000]
                report = self.store.finish_activation(
                    parent,
                    worker_id,
                    activation_id,
                    message["message_id"],
                    state="SUCCEEDED",
                    summary=text,
                    message_state="CONSUMED",
                    worker_state="DORMANT",
                )
                return {
                    "worker_id": worker_id,
                    "activation_id": activation_id,
                    "subagent_id": handle.subagent_id,
                    "status": "SUCCEEDED",
                    "summary": text,
                    "report_message_id": report["message_id"] if report else None,
                }

            error_text = str(error or state or "activation failed")[:32000]
            self.store.finish_activation(
                parent,
                worker_id,
                activation_id,
                message["message_id"],
                state=state or "FAILED",
                error=error_text,
                message_state="FAILED",
                worker_state="FAILED",
            )
            return {
                "worker_id": worker_id,
                "activation_id": activation_id,
                "subagent_id": handle.subagent_id,
                "status": state or "FAILED",
                "error": error_text,
            }
        except Exception as exc:
            self.store.finish_activation(
                parent,
                worker_id,
                activation_id,
                message["message_id"],
                state="FAILED_TO_START",
                error=str(exc)[:32000],
                message_state="PENDING",
                worker_state="DORMANT",
            )
            raise
