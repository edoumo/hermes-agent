"""H6.1 operator preferences for durable workers.

The H6 durable schema already carries editable presentation/runtime fields and a
DISABLED state. This module exposes those capabilities without weakening the
durable-history contract: operators may change safe worker preferences and
archive or restore a dormant identity, but no worker transcript, activation,
task, or audit row is deleted.
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterable, Optional

from agent.durable_workers import DurableWorkerConflictError, DurableWorkerError
from agent.versioned_durable_workers import VersionedDurableWorkerStore

_UNSET = object()


class ManagedVersionedDurableWorkerStore(VersionedDurableWorkerStore):
    """Versioned store with reversible operator-facing worker preferences."""

    @staticmethod
    def _expected_revision(value: Any) -> int:
        try:
            revision = int(value)
        except (TypeError, ValueError) as exc:
            raise DurableWorkerError("expected_revision must be a positive integer") from exc
        if revision < 1:
            raise DurableWorkerError("expected_revision must be a positive integer")
        return revision

    @staticmethod
    def _normalized_toolsets(toolsets: Optional[Iterable[str]]) -> list[str]:
        normalized: list[str] = []
        for raw in toolsets or []:
            name = str(raw or "").strip()
            if not name or len(name) > 100 or any(ch in name for ch in "\r\n\x00"):
                raise DurableWorkerError("toolset names must contain 1..100 safe characters")
            if name not in normalized:
                normalized.append(name)
        if len(normalized) > 64:
            raise DurableWorkerError("toolsets must contain at most 64 names")
        return normalized

    def update_worker_preferences(
        self,
        parent: str,
        worker_id: str,
        *,
        expected_revision: int,
        label: Any = _UNSET,
        model: Any = _UNSET,
        toolsets: Any = _UNSET,
    ) -> dict[str, Any]:
        """Update safe worker preferences with revision CAS.

        Runtime-affecting preferences cannot be changed while the worker is
        RUNNING. ``model=None`` explicitly clears the override and returns the
        worker to the gateway-default model.
        """

        expected = self._expected_revision(expected_revision)
        assignments: list[str] = []
        values: list[Any] = []

        if label is not _UNSET:
            cleaned_label = str(label or "").strip()
            if not cleaned_label or len(cleaned_label) > 160:
                raise DurableWorkerError("label must contain 1..160 characters")
            assignments.append("label=?")
            values.append(cleaned_label)

        if model is not _UNSET:
            if model is None:
                cleaned_model = None
            else:
                cleaned_model = str(model).strip()
                if not cleaned_model:
                    cleaned_model = None
                elif len(cleaned_model) > 512 or any(
                    ch in cleaned_model for ch in "\r\n\x00"
                ):
                    raise DurableWorkerError(
                        "model must contain at most 512 safe characters"
                    )
            assignments.append("model=?")
            values.append(cleaned_model)

        if toolsets is not _UNSET:
            assignments.append("toolsets_json=?")
            values.append(json.dumps(self._normalized_toolsets(toolsets)))

        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            worker = self._owned_worker(db, parent, worker_id)
            if int(worker["revision"]) != expected:
                raise DurableWorkerConflictError("durable worker revision conflict")
            if worker["status"] == "RUNNING":
                raise DurableWorkerConflictError(
                    "cannot edit a durable worker while it is running"
                )
            if not assignments:
                db.commit()
                return self._worker(worker)

            assignments.extend(["updated_at=?", "revision=revision+1"])
            values.extend([time.time(), worker_id, parent, expected])
            updated = db.execute(
                "UPDATE durable_workers SET "
                + ", ".join(assignments)
                + " WHERE worker_id=? AND parent_session_id=? AND revision=?",
                values,
            ).rowcount
            if updated != 1:
                raise DurableWorkerConflictError("durable worker revision conflict")
            row = self._owned_worker(db, parent, worker_id)
            db.commit()
            return self._worker(row)

    def set_worker_archived(
        self,
        parent: str,
        worker_id: str,
        *,
        archived: bool,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Archive or restore a worker without deleting durable history.

        Archiving is deliberately restricted to DORMANT workers. In particular,
        a FAILED worker cannot be archived and restored as a shortcut around
        the qualified retry/recovery path.
        """

        expected = self._expected_revision(expected_revision)
        target = "DISABLED" if archived else "DORMANT"
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            worker = self._owned_worker(db, parent, worker_id)
            if int(worker["revision"]) != expected:
                raise DurableWorkerConflictError("durable worker revision conflict")
            if archived:
                if worker["status"] == "DISABLED":
                    db.commit()
                    return self._worker(worker)
                if worker["status"] != "DORMANT":
                    raise DurableWorkerConflictError(
                        "only a dormant durable worker can be archived"
                    )
            elif worker["status"] != "DISABLED":
                raise DurableWorkerConflictError(
                    "only an archived durable worker can be restored"
                )

            updated = db.execute(
                "UPDATE durable_workers "
                "SET status=?, updated_at=?, revision=revision+1 "
                "WHERE worker_id=? AND parent_session_id=? AND revision=?",
                (target, time.time(), worker_id, parent, expected),
            ).rowcount
            if updated != 1:
                raise DurableWorkerConflictError("durable worker revision conflict")
            row = self._owned_worker(db, parent, worker_id)
            db.commit()
            return self._worker(row)


__all__ = ["ManagedVersionedDurableWorkerStore"]
