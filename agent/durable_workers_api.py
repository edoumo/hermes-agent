"""Read-side API projection for Hermes Durable Workers H2.

This module deliberately does not expose SQLite to clients. It provides a
bounded, parent-scoped projection that HTTP/gateway surfaces can consume.
Writes and activations remain owned by the H1 DurableWorkerService.
"""
from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


class DurableWorkersApiError(ValueError):
    """Base error for public H2 projection requests."""


class InvalidCursorError(DurableWorkersApiError):
    """Raised when a pagination cursor is malformed or used on the wrong feed."""


class NotFoundError(DurableWorkersApiError):
    """Raised when a parent-scoped public object cannot be found."""


@dataclass(frozen=True)
class Page:
    items: list[dict[str, Any]]
    next_cursor: Optional[str]
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
        }


def _encode_cursor(kind: str, updated_at: float, object_id: str) -> str:
    payload = json.dumps(
        {"v": 1, "kind": kind, "ts": float(updated_at), "id": str(object_id)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: Optional[str], kind: str) -> Optional[tuple[float, str]]:
    if not cursor:
        return None
    try:
        raw = str(cursor).strip()
        if not raw or len(raw) > 1024:
            raise ValueError
        padding = "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw + padding).decode("utf-8"))
        if payload.get("v") != 1 or payload.get("kind") != kind:
            raise ValueError
        ts = float(payload["ts"])
        object_id = str(payload["id"])
        if not object_id or len(object_id) > 256:
            raise ValueError
        return ts, object_id
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
        raise InvalidCursorError("Invalid pagination cursor.") from exc


def _bounded_limit(limit: Any, *, default: int = 50, maximum: int = 100) -> int:
    if limit is None:
        return default
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise DurableWorkersApiError("limit must be an integer") from exc
    if value < 1 or value > maximum:
        raise DurableWorkersApiError(f"limit must be between 1 and {maximum}")
    return value


class DurableWorkersProjection:
    """Bounded read projection over the experimental H1 SQLite store.

    Every query is scoped by ``parent_session_id`` before applying caller
    pagination. Cursors are intentionally opaque transport tokens, not
    authorization capabilities; changing one can only move within the same
    parent-scoped feed.
    """

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise NotFoundError("Durable worker store is not initialized.")
        uri = f"file:{self.db_path.resolve()}?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=1.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db

    @staticmethod
    def _worker(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        raw_toolsets = out.pop("toolsets_json", None)
        try:
            out["toolsets"] = json.loads(raw_toolsets or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            out["toolsets"] = []
        return out

    @staticmethod
    def _public_activation(row: sqlite3.Row) -> dict[str, Any]:
        # Owner PID/birth markers are host-internal recovery details and are
        # deliberately excluded from the public UI/API projection.
        allowed = (
            "activation_id",
            "worker_id",
            "message_id",
            "subagent_id",
            "state",
            "started_at",
            "completed_at",
            "summary",
            "error",
        )
        return {key: row[key] for key in allowed}

    def _worker_exists(self, db: sqlite3.Connection, parent: str, worker_id: str) -> None:
        row = db.execute(
            "SELECT 1 FROM durable_workers WHERE parent_session_id=? AND worker_id=?",
            (parent, worker_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("Durable worker not found for this session.")

    def list_workers(
        self,
        parent: str,
        *,
        limit: Any = None,
        cursor: Optional[str] = None,
    ) -> Page:
        page_size = _bounded_limit(limit)
        after = _decode_cursor(cursor, "workers")
        sql = "SELECT * FROM durable_workers WHERE parent_session_id=? "
        args: list[Any] = [parent]
        if after is not None:
            sql += "AND (updated_at < ? OR (updated_at = ? AND worker_id < ?)) "
            args.extend([after[0], after[0], after[1]])
        sql += "ORDER BY updated_at DESC, worker_id DESC LIMIT ?"
        args.append(page_size + 1)
        with self._connect() as db:
            rows = db.execute(sql, args).fetchall()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        items = [self._worker(row) for row in rows]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = _encode_cursor("workers", last["updated_at"], last["worker_id"])
        return Page(items, next_cursor, has_more)

    def get_worker(self, parent: str, worker_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM durable_workers WHERE parent_session_id=? AND worker_id=?",
                (parent, worker_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("Durable worker not found for this session.")
        return self._worker(row)

    def list_messages(
        self,
        parent: str,
        worker_id: str,
        *,
        limit: Any = None,
        cursor: Optional[str] = None,
    ) -> Page:
        page_size = _bounded_limit(limit)
        after = _decode_cursor(cursor, "messages")
        sql = (
            "SELECT m.* FROM durable_worker_messages m "
            "JOIN durable_workers w ON w.worker_id=m.worker_id "
            "WHERE w.parent_session_id=? AND m.worker_id=? "
        )
        args: list[Any] = [parent, worker_id]
        if after is not None:
            sql += "AND (m.created_at < ? OR (m.created_at = ? AND m.message_id < ?)) "
            args.extend([after[0], after[0], after[1]])
        sql += "ORDER BY m.created_at DESC, m.message_id DESC LIMIT ?"
        args.append(page_size + 1)
        with self._connect() as db:
            self._worker_exists(db, parent, worker_id)
            rows = db.execute(sql, args).fetchall()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        items = [dict(row) for row in rows]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = _encode_cursor("messages", last["created_at"], last["message_id"])
        return Page(items, next_cursor, has_more)

    def list_activations(
        self,
        parent: str,
        worker_id: str,
        *,
        limit: Any = None,
        cursor: Optional[str] = None,
    ) -> Page:
        page_size = _bounded_limit(limit)
        after = _decode_cursor(cursor, "activations")
        sql = (
            "SELECT a.* FROM durable_worker_activations a "
            "JOIN durable_workers w ON w.worker_id=a.worker_id "
            "WHERE w.parent_session_id=? AND a.worker_id=? "
        )
        args: list[Any] = [parent, worker_id]
        if after is not None:
            sql += "AND (a.started_at < ? OR (a.started_at = ? AND a.activation_id < ?)) "
            args.extend([after[0], after[0], after[1]])
        sql += "ORDER BY a.started_at DESC, a.activation_id DESC LIMIT ?"
        args.append(page_size + 1)
        with self._connect() as db:
            self._worker_exists(db, parent, worker_id)
            rows = db.execute(sql, args).fetchall()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        items = [self._public_activation(row) for row in rows]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = _encode_cursor(
                "activations", last["started_at"], last["activation_id"]
            )
        return Page(items, next_cursor, has_more)

    def list_tasks(
        self,
        parent: str,
        *,
        limit: Any = None,
        cursor: Optional[str] = None,
    ) -> Page:
        page_size = _bounded_limit(limit)
        after = _decode_cursor(cursor, "tasks")
        sql = "SELECT * FROM durable_worker_tasks WHERE parent_session_id=? "
        args: list[Any] = [parent]
        if after is not None:
            sql += "AND (updated_at < ? OR (updated_at = ? AND task_id < ?)) "
            args.extend([after[0], after[0], after[1]])
        sql += "ORDER BY updated_at DESC, task_id DESC LIMIT ?"
        args.append(page_size + 1)
        with self._connect() as db:
            rows = db.execute(sql, args).fetchall()
            items = []
            for row in rows[:page_size]:
                item = dict(row)
                blockers = db.execute(
                    "SELECT blocked_by_task_id FROM durable_worker_task_dependencies "
                    "WHERE task_id=? ORDER BY blocked_by_task_id",
                    (row["task_id"],),
                ).fetchall()
                item["blocked_by"] = [blocker[0] for blocker in blockers]
                items.append(item)
        has_more = len(rows) > page_size
        next_cursor = None
        if has_more and rows[:page_size]:
            last = rows[page_size - 1]
            next_cursor = _encode_cursor("tasks", last["updated_at"], last["task_id"])
        return Page(items, next_cursor, has_more)
