"""H6 final-consolidation adapter for the Durable Workers API.

H6 does not add routes or listeners. It preserves the qualified H5 API surface
and replaces only the internal store factory with the versioned storage
contract used for final integration qualification.
"""
from __future__ import annotations

from agent.versioned_durable_workers import VersionedDurableWorkerStore
from gateway.platforms.api_server_durable_task_recovery import (
    DurableWorkersTaskRecoveryAPIServerAdapter,
)


class DurableWorkersFinalAPIServerAdapter(
    DurableWorkersTaskRecoveryAPIServerAdapter
):
    """Qualified H5 surface backed by the H6 versioned durable store."""

    def _durable_worker_store(self) -> VersionedDurableWorkerStore:
        return VersionedDurableWorkerStore(self._durable_worker_db_path())


__all__ = ["DurableWorkersFinalAPIServerAdapter"]
