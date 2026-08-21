"""H6 final-consolidation adapter for the Durable Workers API.

H6 does not add routes or listeners. It preserves the qualified H5 API surface
and replaces only the internal store factory with the versioned storage
contract used for final integration qualification.
"""
from __future__ import annotations

from agent.durable_worker_schema import DURABLE_SCHEMA_VERSION
from agent.versioned_durable_workers import VersionedDurableWorkerStore
from gateway.platforms.api_server_durable_task_recovery import (
    DurableWorkersTaskRecoveryAPIServerAdapter,
)


class DurableWorkersFinalAPIServerAdapter(
    DurableWorkersTaskRecoveryAPIServerAdapter
):
    """Qualified H5 surface backed by the H6 versioned durable store."""

    def __init__(self, config):
        super().__init__(config)
        # Durable Workers is explicitly opt-in. Validate/adopt its database
        # during adapter construction so incompatible storage fails before the
        # API listener starts rather than on the first operator request.
        self._dw_versioned_store = VersionedDurableWorkerStore(
            self._durable_worker_db_path()
        )
        self._dw_storage_schema_version = DURABLE_SCHEMA_VERSION

    def _durable_worker_store(self) -> VersionedDurableWorkerStore:
        # Store objects are connectionless wrappers around a database path;
        # individual operations still open their own SQLite connections. A
        # single wrapper therefore avoids repeating schema bootstrap/audit on
        # every API call without introducing shared connection state.
        store = getattr(self, "_dw_versioned_store", None)
        if store is None:
            store = VersionedDurableWorkerStore(self._durable_worker_db_path())
            self._dw_versioned_store = store
        return store


__all__ = ["DurableWorkersFinalAPIServerAdapter"]
