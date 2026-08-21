"""H6 versioned Durable Worker store.

The qualified H1 store remains the implementation of worker/message/task state.
This subclass adds only the final storage lifecycle contract: reject future
schemas before mutation, bootstrap the inherited H1 tables, adopt/validate the
formal H6 schema, then run the existing abandoned-activation recovery.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from agent.durable_worker_schema import (
    ensure_current_schema,
    refuse_future_schema,
)
from agent.durable_workers import DurableWorkerStore, _default_path


class VersionedDurableWorkerStore(DurableWorkerStore):
    """DurableWorkerStore with H6 schema compatibility guarantees."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else _default_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # This read-only guard must happen before inherited schema bootstrap.
        refuse_future_schema(self.db_path)

        # Preserve the qualified H1 additive bootstrap, then formally adopt the
        # complete H5 layout as schema version 1 before state recovery mutates
        # any abandoned activation/message/worker rows.
        self._init_schema()
        ensure_current_schema(self.db_path)
        self.recover_abandoned_activations()


__all__ = ["VersionedDurableWorkerStore"]
