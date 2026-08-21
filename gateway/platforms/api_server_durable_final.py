"""H6 final-consolidation adapter for the Durable Workers API.

H6.1 keeps the qualified H5/H6 execution surface and adds only reversible
operator controls discovered during user acceptance testing: worker preference
editing plus archive/restore.  No listener, scheduler, or storage schema is
added.
"""
from __future__ import annotations

import asyncio
from typing import Any

from agent.durable_worker_preferences import ManagedVersionedDurableWorkerStore
from agent.durable_worker_schema import DURABLE_SCHEMA_VERSION
from agent.durable_workers import DurableWorkerError
from gateway.platforms.api_server import _openai_error, web
from gateway.platforms.api_server_durable_task_recovery import (
    DurableWorkersTaskRecoveryAPIServerAdapter,
)


class DurableWorkersFinalAPIServerAdapter(
    DurableWorkersTaskRecoveryAPIServerAdapter
):
    """Qualified H5 surface backed by H6 storage plus reversible UAT controls."""

    _H61_WORKER_ROUTES = (
        (
            "POST",
            "/api/sessions/{session_id}/workers/{worker_id}/edit",
            "_handle_dw_edit_worker",
        ),
        (
            "POST",
            "/api/sessions/{session_id}/workers/{worker_id}/archive",
            "_handle_dw_archive_worker",
        ),
        (
            "POST",
            "/api/sessions/{session_id}/workers/{worker_id}/restore",
            "_handle_dw_restore_worker",
        ),
    )

    def __init__(self, config):
        super().__init__(config)
        # Durable Workers is explicitly opt-in. Validate/adopt its database
        # during adapter construction so incompatible storage fails before the
        # API listener starts rather than on the first operator request.
        self._dw_versioned_store = ManagedVersionedDurableWorkerStore(
            self._durable_worker_db_path()
        )
        self._dw_storage_schema_version = DURABLE_SCHEMA_VERSION

    def _http_route_table(self) -> list[tuple]:
        routes = list(super()._http_route_table())
        routes.extend(
            (method, path, getattr(self, handler_name))
            for method, path, handler_name in self._H61_WORKER_ROUTES
        )
        return routes

    def _durable_worker_store(self) -> ManagedVersionedDurableWorkerStore:
        # Store objects are connectionless wrappers around a database path;
        # individual operations still open their own SQLite connections. A
        # single wrapper therefore avoids repeating schema bootstrap/audit on
        # every API call without introducing shared connection state.
        store = getattr(self, "_dw_versioned_store", None)
        if store is None:
            store = ManagedVersionedDurableWorkerStore(
                self._durable_worker_db_path()
            )
            self._dw_versioned_store = store
        return store

    @staticmethod
    def _h61_expected_revision(body: dict[str, Any]) -> int:
        value = body.get("expected_revision")
        try:
            revision = int(value)
        except (TypeError, ValueError) as exc:
            raise DurableWorkerError(
                "expected_revision must be a positive integer"
            ) from exc
        if revision < 1:
            raise DurableWorkerError("expected_revision must be a positive integer")
        return revision

    async def _handle_dw_edit_worker(self, request: "web.Request") -> "web.Response":
        session_id, _session, err = await self._dw_session_or_error(request)
        if err:
            return err
        worker_id = str(request.match_info.get("worker_id") or "").strip()
        body, err = await self._read_json_body(request)
        if err:
            return err
        if not isinstance(body, dict):
            return web.json_response(
                _openai_error("Request body must be a JSON object"), status=400
            )
        unknown = self._dw_unknown_fields(
            body,
            {"label", "model", "toolsets", "expected_revision"},
        )
        if unknown:
            return unknown
        if not ({"label", "model", "toolsets"} & set(body)):
            return web.json_response(
                _openai_error(
                    "At least one editable worker field is required",
                    code="invalid_durable_worker_request",
                ),
                status=400,
            )
        try:
            expected_revision = self._h61_expected_revision(body)
            kwargs: dict[str, Any] = {"expected_revision": expected_revision}
            if "label" in body:
                kwargs["label"] = body.get("label")
            if "model" in body:
                model_raw = body.get("model")
                if model_raw is not None and not isinstance(model_raw, str):
                    raise DurableWorkerError("model must be a string or null")
                kwargs["model"] = model_raw
            if "toolsets" in body:
                kwargs["toolsets"] = self._dw_normalize_toolsets(body.get("toolsets"))
            store = self._durable_worker_store()
            worker = await asyncio.to_thread(
                store.update_worker_preferences,
                session_id,
                worker_id,
                **kwargs,
            )
        except Exception as exc:
            return self._dw_error_response(exc)
        return web.json_response(
            {
                "object": "hermes.durable_worker",
                "session_id": session_id,
                "worker": worker,
            }
        )

    async def _handle_dw_archive_worker(self, request: "web.Request") -> "web.Response":
        return await self._handle_dw_archive_state(request, archived=True)

    async def _handle_dw_restore_worker(self, request: "web.Request") -> "web.Response":
        return await self._handle_dw_archive_state(request, archived=False)

    async def _handle_dw_archive_state(
        self,
        request: "web.Request",
        *,
        archived: bool,
    ) -> "web.Response":
        session_id, _session, err = await self._dw_session_or_error(request)
        if err:
            return err
        worker_id = str(request.match_info.get("worker_id") or "").strip()
        body, err = await self._read_json_body(request)
        if err:
            return err
        if not isinstance(body, dict):
            return web.json_response(
                _openai_error("Request body must be a JSON object"), status=400
            )
        unknown = self._dw_unknown_fields(body, {"expected_revision"})
        if unknown:
            return unknown
        try:
            expected_revision = self._h61_expected_revision(body)
            worker = await asyncio.to_thread(
                self._durable_worker_store().set_worker_archived,
                session_id,
                worker_id,
                archived=archived,
                expected_revision=expected_revision,
            )
        except Exception as exc:
            return self._dw_error_response(exc)
        return web.json_response(
            {
                "object": "hermes.durable_worker",
                "session_id": session_id,
                "worker": worker,
                "archived": archived,
            }
        )


__all__ = ["DurableWorkersFinalAPIServerAdapter"]
