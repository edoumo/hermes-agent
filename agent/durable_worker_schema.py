"""Versioned SQLite schema contract for Hermes Durable Workers.

H6 deliberately formalizes the already-qualified H5 storage layout instead of
introducing a destructive migration. Schema version 1 is the H5-compatible
layout: H1/H4 worker, message, activation and task tables plus the H5 task-run
audit table.

Legacy H1-H5 databases used ``PRAGMA user_version = 0``. H6 validates their
shape, creates the additive H5 audit table when absent, then atomically adopts
them as version 1. A database created by a future schema version is rejected
read-only before the H6 store performs any mutation.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Mapping
from urllib.parse import quote

DURABLE_SCHEMA_VERSION = 1

_TASK_RUNS_DDL = """
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

_REQUIRED_COLUMNS: Mapping[str, frozenset[str]] = {
    "durable_workers": frozenset(
        {
            "worker_id",
            "parent_session_id",
            "label",
            "status",
            "role",
            "model",
            "toolsets_json",
            "created_at",
            "updated_at",
            "revision",
            "last_activation_id",
        }
    ),
    "durable_worker_messages": frozenset(
        {
            "message_id",
            "worker_id",
            "direction",
            "content",
            "state",
            "created_at",
            "updated_at",
        }
    ),
    "durable_worker_activations": frozenset(
        {
            "activation_id",
            "worker_id",
            "message_id",
            "subagent_id",
            "state",
            "started_at",
            "completed_at",
            "summary",
            "error",
            "owner_pid",
            "owner_started_at",
        }
    ),
    "durable_worker_tasks": frozenset(
        {
            "task_id",
            "parent_session_id",
            "worker_id",
            "subject",
            "description",
            "status",
            "revision",
            "created_at",
            "updated_at",
        }
    ),
    "durable_worker_task_dependencies": frozenset(
        {"task_id", "blocked_by_task_id"}
    ),
    "durable_worker_task_runs": frozenset(
        {
            "activation_id",
            "task_id",
            "message_id",
            "worker_id",
            "state",
            "created_at",
            "completed_at",
            "summary",
            "error",
        }
    ),
}


class DurableWorkerSchemaError(RuntimeError):
    """The durable database is incompatible with the current schema contract."""


@dataclass(frozen=True)
class DurableWorkerSchemaAudit:
    """Read-only result of an explicit durable database integrity audit."""

    version: int
    quick_check: str
    foreign_key_violations: tuple[tuple[object, ...], ...]
    table_columns: Mapping[str, tuple[str, ...]]

    @property
    def ok(self) -> bool:
        return (
            self.version == DURABLE_SCHEMA_VERSION
            and self.quick_check == "ok"
            and not self.foreign_key_violations
        )


def _readonly_uri(path: Path) -> str:
    normalized = quote(str(path.resolve()), safe="/:")
    return f"file:{normalized}?mode=ro"


def _read_version(db: sqlite3.Connection) -> int:
    return int(db.execute("PRAGMA user_version").fetchone()[0])


def refuse_future_schema(db_path: Path) -> int:
    """Reject a future schema before any H6 write is possible.

    Missing databases are treated as unversioned and are created later by the
    normal H1 store bootstrap.
    """

    path = Path(db_path)
    if not path.exists():
        return 0
    try:
        with sqlite3.connect(_readonly_uri(path), uri=True, timeout=2.0) as db:
            version = _read_version(db)
    except sqlite3.DatabaseError as exc:
        raise DurableWorkerSchemaError(
            f"unable to read durable worker schema version: {exc}"
        ) from exc
    if version > DURABLE_SCHEMA_VERSION:
        raise DurableWorkerSchemaError(
            "durable worker database schema is newer than this Hermes build "
            f"(database={version}, supported={DURABLE_SCHEMA_VERSION})"
        )
    return version


def _table_columns(db: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in db.execute(f"PRAGMA table_info({table})"))


def _validate_required_shape(db: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    observed: dict[str, tuple[str, ...]] = {}
    for table, required in _REQUIRED_COLUMNS.items():
        columns = _table_columns(db, table)
        observed[table] = columns
        missing = sorted(required.difference(columns))
        if missing:
            raise DurableWorkerSchemaError(
                f"durable worker table {table!r} is missing required columns: "
                + ", ".join(missing)
            )
    return observed


def ensure_current_schema(db_path: Path) -> int:
    """Adopt the H5-compatible layout as formal schema version 1.

    The caller is expected to have run the inherited H1 ``_init_schema`` first,
    so all base tables exist and the historical ``owner_started_at`` additive
    migration has already been applied. H6 only centralizes the H5 task-run
    table, validates the complete shape and stamps ``PRAGMA user_version``.
    """

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5.0)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        version = _read_version(db)
        if version > DURABLE_SCHEMA_VERSION:
            raise DurableWorkerSchemaError(
                "durable worker database schema is newer than this Hermes build "
                f"(database={version}, supported={DURABLE_SCHEMA_VERSION})"
            )
        db.execute(_TASK_RUNS_DDL)
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dwtr_task "
            "ON durable_worker_task_runs(task_id, created_at DESC)"
        )
        _validate_required_shape(db)
        violations = tuple(tuple(row) for row in db.execute("PRAGMA foreign_key_check"))
        if violations:
            raise DurableWorkerSchemaError(
                "durable worker database contains foreign-key violations"
            )
        if version < DURABLE_SCHEMA_VERSION:
            db.execute(f"PRAGMA user_version={DURABLE_SCHEMA_VERSION}")
        db.commit()
        return DURABLE_SCHEMA_VERSION
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def audit_schema(db_path: Path) -> DurableWorkerSchemaAudit:
    """Run a read-only structural, foreign-key and SQLite quick-check audit."""

    path = Path(db_path)
    if not path.exists():
        raise DurableWorkerSchemaError("durable worker database does not exist")
    try:
        with sqlite3.connect(_readonly_uri(path), uri=True, timeout=5.0) as db:
            version = _read_version(db)
            if version != DURABLE_SCHEMA_VERSION:
                raise DurableWorkerSchemaError(
                    "durable worker database schema version mismatch "
                    f"(database={version}, expected={DURABLE_SCHEMA_VERSION})"
                )
            observed = _validate_required_shape(db)
            violations = tuple(
                tuple(row) for row in db.execute("PRAGMA foreign_key_check")
            )
            quick_rows = tuple(
                str(row[0]) for row in db.execute("PRAGMA quick_check")
            )
    except sqlite3.DatabaseError as exc:
        raise DurableWorkerSchemaError(
            f"unable to audit durable worker database: {exc}"
        ) from exc
    quick_check = "ok" if quick_rows == ("ok",) else "; ".join(quick_rows)
    return DurableWorkerSchemaAudit(
        version=version,
        quick_check=quick_check,
        foreign_key_violations=violations,
        table_columns=observed,
    )


__all__ = [
    "DURABLE_SCHEMA_VERSION",
    "DurableWorkerSchemaAudit",
    "DurableWorkerSchemaError",
    "audit_schema",
    "ensure_current_schema",
    "refuse_future_schema",
]
