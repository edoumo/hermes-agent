"""Experimental H2 API-server extension for Durable Workers.

The class subclasses Hermes' existing authenticated API server and only adds
session-scoped, read-only Durable Worker routes. It is intentionally not wired
as the default API-server adapter yet; H2 can qualify the HTTP contract before
changing the gateway factory.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from agent.durable_workers_api import (
    DurableWorkersApiError,
    DurableWorkersProjection,
    InvalidCursorError,
    NotFoundError,
    Page,
)
from gateway.platforms.api_server import APIServerAdapter, _openai_error, web


class DurableWorkersAPIServerAdapter(APIServerAdapter):
    """APIServerAdapter with bounded Durable Worker read routes."""

    _DURABLE_WORKER_ROUTES = (
        ("GET", "/api/sessions/{session_id}/workers", "_handle_dw_list_workers"),
        ("GET", "/api/sessions/{session_id}/workers/{worker_id}", "_handle_dw_get_worker"),
        ("GET", "/api/sessions/{session_id}/workers/{worker_id}/messages", "_handle_dw_messages"),
        ("GET", "/api/sessions/{session_id}/workers/{worker_id}/activations", "_handle_dw_activations"),
        ("GET", "/api/sessions/{session_id}/worker-tasks", "_handle_dw_tasks"),
    )

    def _http_route_table(self) -> list[tuple]:
        routes = list(super()._http_route_table())
        routes.extend(
            (method, path, getattr(self, handler_name))
            for method, path, handler_name in self._DURABLE_WORKER_ROUTES
        )
        return routes

    @staticmethod
    def _durable_worker_db_path() -> Path:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "durable-workers.db"

    def _durable_worker_projection(self) -> DurableWorkersProjection:
        return DurableWorkersProjection(self._durable_worker_db_path())

    async def _dw_session_or_error(
        self, request: "web.Request"
    ) -> tuple[Optional[str], Optional["web.Response"]]:
        auth_err = self._check_auth(request)
        if auth_err:
            return None, auth_err
        session_id = str(request.match_info.get("session_id") or "").strip()
        _session, err = await self._get_existing_session_or_404(session_id)
        if err:
            return None, err
        return session_id, None

    @staticmethod
    def _dw_page_response(
        session_id: str, page: Page, *, object_name: str
    ) -> "web.Response":
        payload = page.to_dict()
        payload.update({"object": object_name, "session_id": session_id})
        return web.json_response(payload)

    @staticmethod
    def _dw_error_response(exc: Exception) -> "web.Response":
        if isinstance(exc, NotFoundError):
            return web.json_response(
                _openai_error(str(exc), code="durable_worker_not_found"), status=404
            )
        if isinstance(exc, (InvalidCursorError, DurableWorkersApiError)):
            return web.json_response(
                _openai_error(str(exc), code="invalid_durable_worker_request"),
                status=400,
            )
        return web.json_response(
            _openai_error("Durable Worker projection unavailable", code="durable_worker_unavailable"),
            status=503,
        )

    async def _handle_dw_list_workers(self, request: "web.Request") -> "web.Response":
        session_id, err = await self._dw_session_or_error(request)
        if err:
            return err
        db_path = self._durable_worker_db_path()
        if not db_path.exists():
            return self._dw_page_response(
                session_id,
                Page([], None, False),
                object_name="hermes.durable_worker.list",
            )
        try:
            page = await asyncio.to_thread(
                self._durable_worker_projection().list_workers,
                session_id,
                limit=request.query.get("limit"),
                cursor=request.query.get("cursor"),
            )
        except Exception as exc:
            return self._dw_error_response(exc)
        return self._dw_page_response(
            session_id, page, object_name="hermes.durable_worker.list"
        )

    async def _handle_dw_get_worker(self, request: "web.Request") -> "web.Response":
        session_id, err = await self._dw_session_or_error(request)
        if err:
            return err
        worker_id = str(request.match_info.get("worker_id") or "").strip()
        try:
            worker = await asyncio.to_thread(
                self._durable_worker_projection().get_worker, session_id, worker_id
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

    async def _handle_dw_messages(self, request: "web.Request") -> "web.Response":
        session_id, err = await self._dw_session_or_error(request)
        if err:
            return err
        worker_id = str(request.match_info.get("worker_id") or "").strip()
        try:
            page = await asyncio.to_thread(
                self._durable_worker_projection().list_messages,
                session_id,
                worker_id,
                limit=request.query.get("limit"),
                cursor=request.query.get("cursor"),
            )
        except Exception as exc:
            return self._dw_error_response(exc)
        return self._dw_page_response(
            session_id, page, object_name="hermes.durable_worker_message.list"
        )

    async def _handle_dw_activations(self, request: "web.Request") -> "web.Response":
        session_id, err = await self._dw_session_or_error(request)
        if err:
            return err
        worker_id = str(request.match_info.get("worker_id") or "").strip()
        try:
            page = await asyncio.to_thread(
                self._durable_worker_projection().list_activations,
                session_id,
                worker_id,
                limit=request.query.get("limit"),
                cursor=request.query.get("cursor"),
            )
        except Exception as exc:
            return self._dw_error_response(exc)
        return self._dw_page_response(
            session_id, page, object_name="hermes.durable_worker_activation.list"
        )

    async def _handle_dw_tasks(self, request: "web.Request") -> "web.Response":
        session_id, err = await self._dw_session_or_error(request)
        if err:
            return err
        db_path = self._durable_worker_db_path()
        if not db_path.exists():
            return self._dw_page_response(
                session_id,
                Page([], None, False),
                object_name="hermes.durable_worker_task.list",
            )
        try:
            page = await asyncio.to_thread(
                self._durable_worker_projection().list_tasks,
                session_id,
                limit=request.query.get("limit"),
                cursor=request.query.get("cursor"),
            )
        except Exception as exc:
            return self._dw_error_response(exc)
        return self._dw_page_response(
            session_id, page, object_name="hermes.durable_worker_task.list"
        )
