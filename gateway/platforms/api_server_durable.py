"""Experimental H2 API-server extension for Hermes Durable Workers.

The extension subclasses Hermes' authenticated API server. H2.1 adds a
session-scoped control plane and an incremental SSE invalidation stream while
keeping the durable SQLite store internal to Hermes.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from agent.durable_worker_execution import execute_reserved_activation
from agent.durable_workers import (
    DurableWorkerAuthorizationError,
    DurableWorkerConflictError,
    DurableWorkerError,
    DurableWorkerStore,
)
from agent.durable_workers_api import (
    DurableWorkersApiError,
    DurableWorkersProjection,
    InvalidCursorError,
    NotFoundError,
    Page,
)
from gateway.platforms.api_server import (
    APIServerAdapter,
    _api_request_profile,
    _openai_error,
    _sse_frame,
    web,
)
from tools.daemon_pool import DaemonThreadPoolExecutor

logger = logging.getLogger(__name__)

_DW_EXECUTOR = DaemonThreadPoolExecutor(
    max_workers=8, thread_name_prefix="hermes-durable-api"
)
_DW_SSE_POLL_SECONDS = 1.0
_DW_SSE_KEEPALIVE_SECONDS = 20.0
_DW_MAX_EVENT_SUBSCRIBERS = 16
_IDENTIFIER_RE = re.compile(r"^[^\r\n\x00]{1,256}$")


class DurableWorkersAPIServerAdapter(APIServerAdapter):
    """APIServerAdapter with bounded Durable Worker read/write routes."""

    _DURABLE_WORKER_ROUTES = (
        ("GET", "/api/sessions/{session_id}/workers", "_handle_dw_list_workers"),
        ("POST", "/api/sessions/{session_id}/workers", "_handle_dw_create_worker"),
        ("GET", "/api/sessions/{session_id}/worker-events", "_handle_dw_events"),
        ("GET", "/api/sessions/{session_id}/workers/{worker_id}", "_handle_dw_get_worker"),
        ("GET", "/api/sessions/{session_id}/workers/{worker_id}/messages", "_handle_dw_messages"),
        ("POST", "/api/sessions/{session_id}/workers/{worker_id}/messages", "_handle_dw_enqueue_message"),
        ("POST", "/api/sessions/{session_id}/workers/{worker_id}/run", "_handle_dw_run"),
        ("GET", "/api/sessions/{session_id}/workers/{worker_id}/activations", "_handle_dw_activations"),
        ("GET", "/api/sessions/{session_id}/worker-tasks", "_handle_dw_tasks"),
        ("POST", "/api/sessions/{session_id}/worker-tasks", "_handle_dw_create_task"),
        ("POST", "/api/sessions/{session_id}/worker-tasks/{task_id}/status", "_handle_dw_update_task"),
        ("POST", "/api/sessions/{session_id}/worker-tasks/{task_id}/dependencies", "_handle_dw_add_dependency"),
    )

    def __init__(self, config):
        super().__init__(config)
        extra = getattr(config, "extra", {}) or {}
        try:
            requested = int(extra.get("durable_workers_max_concurrent_activations", 4))
        except (TypeError, ValueError):
            requested = 4
        self._dw_max_concurrent_activations = max(1, min(requested, 8))
        self._dw_activation_tasks: set[asyncio.Task] = set()
        self._dw_dispatch_lock: Optional[asyncio.Lock] = None
        self._dw_event_subscribers = 0

    def _http_route_table(self) -> list[tuple]:
        routes = list(super()._http_route_table())
        routes.extend(
            (method, path, getattr(self, handler_name))
            for method, path, handler_name in self._DURABLE_WORKER_ROUTES
        )
        return routes

    def active_agent_work_count(self) -> int:
        base = super().active_agent_work_count()
        return base + sum(not task.done() for task in self._dw_activation_tasks)

    @staticmethod
    def _durable_worker_db_path() -> Path:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "durable-workers.db"

    def _durable_worker_projection(self) -> DurableWorkersProjection:
        return DurableWorkersProjection(self._durable_worker_db_path())

    def _durable_worker_store(self) -> DurableWorkerStore:
        return DurableWorkerStore(self._durable_worker_db_path())

    async def _dw_session_or_error(
        self, request: "web.Request"
    ) -> tuple[Optional[str], Optional[dict[str, Any]], Optional["web.Response"]]:
        auth_err = self._check_auth(request)
        if auth_err:
            return None, None, auth_err
        session_id = str(request.match_info.get("session_id") or "").strip()
        session, err = await self._get_existing_session_or_404(session_id)
        if err:
            return None, None, err
        return session_id, session, None

    @staticmethod
    def _dw_page_response(
        session_id: str, page: Page, *, object_name: str
    ) -> "web.Response":
        payload = page.to_dict()
        payload.update({"object": object_name, "session_id": session_id})
        return web.json_response(payload)

    @staticmethod
    def _dw_error_response(exc: Exception) -> "web.Response":
        if isinstance(exc, (NotFoundError, DurableWorkerAuthorizationError)):
            return web.json_response(
                _openai_error(
                    "Durable Worker object not found for this session.",
                    code="durable_worker_not_found",
                ),
                status=404,
            )
        if isinstance(exc, DurableWorkerConflictError):
            return web.json_response(
                _openai_error(str(exc), code="durable_worker_conflict"), status=409
            )
        if isinstance(exc, (InvalidCursorError, DurableWorkersApiError, DurableWorkerError)):
            return web.json_response(
                _openai_error(str(exc), code="invalid_durable_worker_request"),
                status=400,
            )
        logger.exception("Durable Worker API operation failed", exc_info=exc)
        return web.json_response(
            _openai_error(
                "Durable Worker service unavailable", code="durable_worker_unavailable"
            ),
            status=503,
        )

    @staticmethod
    def _dw_unknown_fields(
        body: dict[str, Any], allowed: set[str]
    ) -> Optional["web.Response"]:
        unknown = sorted(set(body) - allowed)
        if not unknown:
            return None
        return web.json_response(
            _openai_error(
                f"Unsupported Durable Worker fields: {', '.join(unknown)}",
                code="unsupported_durable_worker_field",
            ),
            status=400,
        )

    @staticmethod
    def _dw_identifier(value: Any, *, field: str, required: bool = True) -> Optional[str]:
        if value is None and not required:
            return None
        text = str(value or "").strip()
        if not text and not required:
            return None
        if not _IDENTIFIER_RE.fullmatch(text):
            raise DurableWorkerError(f"{field} must contain 1..256 safe characters")
        return text

    @staticmethod
    def _dw_normalize_toolsets(value: Any) -> Optional[list[str]]:
        if value is None:
            return None
        if not isinstance(value, list) or len(value) > 64:
            raise DurableWorkerError("toolsets must be an array of at most 64 names")
        normalized: list[str] = []
        for raw in value:
            name = str(raw or "").strip()
            if not name or len(name) > 100 or re.search(r"[\r\n\x00]", name):
                raise DurableWorkerError("toolset names must contain 1..100 safe characters")
            if name not in normalized:
                normalized.append(name)
        from toolsets import TOOLSETS

        unknown = sorted(set(normalized) - set(TOOLSETS))
        if unknown:
            raise DurableWorkerError(f"Unknown toolsets: {', '.join(unknown)}")
        from gateway.run import _load_gateway_config
        from hermes_cli.tools_config import _get_platform_tools

        enabled = set(_get_platform_tools(_load_gateway_config(), "api_server"))
        broadened = sorted(set(normalized) - enabled)
        if broadened:
            raise DurableWorkerError(
                "Requested toolsets would broaden api_server permissions: "
                + ", ".join(broadened)
            )
        return normalized

    async def _handle_dw_list_workers(self, request: "web.Request") -> "web.Response":
        session_id, _session, err = await self._dw_session_or_error(request)
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

    async def _handle_dw_create_worker(self, request: "web.Request") -> "web.Response":
        session_id, _session, err = await self._dw_session_or_error(request)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        if not isinstance(body, dict):
            return web.json_response(
                _openai_error("Request body must be a JSON object"), status=400
            )
        unknown = self._dw_unknown_fields(body, {"label", "role", "model", "toolsets"})
        if unknown:
            return unknown
        try:
            label = str(body.get("label") or "").strip()
            role = str(body.get("role") or "leaf").strip()
            model_raw = body.get("model")
            if model_raw is not None and not isinstance(model_raw, str):
                raise DurableWorkerError("model must be a string or null")
            model = str(model_raw).strip() if model_raw is not None else None
            if model == "":
                model = None
            if model is not None and (
                len(model) > 512 or re.search(r"[\r\n\x00]", model)
            ):
                raise DurableWorkerError("model must contain at most 512 safe characters")
            toolsets = self._dw_normalize_toolsets(body.get("toolsets"))
            store = await asyncio.to_thread(self._durable_worker_store)
            worker = await asyncio.to_thread(
                store.create_worker,
                session_id,
                label=label,
                role=role,
                model=model,
                toolsets=toolsets,
            )
        except Exception as exc:
            return self._dw_error_response(exc)
        return web.json_response(
            {
                "object": "hermes.durable_worker",
                "session_id": session_id,
                "worker": worker,
            },
            status=201,
        )

    async def _handle_dw_get_worker(self, request: "web.Request") -> "web.Response":
        session_id, _session, err = await self._dw_session_or_error(request)
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
        session_id, _session, err = await self._dw_session_or_error(request)
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

    async def _handle_dw_enqueue_message(self, request: "web.Request") -> "web.Response":
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
        unknown = self._dw_unknown_fields(body, {"message", "message_id"})
        if unknown:
            return unknown
        try:
            message = str(body.get("message") or "").strip()
            message_id = self._dw_identifier(
                body.get("message_id"), field="message_id", required=False
            )
            store = await asyncio.to_thread(self._durable_worker_store)
            queued = await asyncio.to_thread(
                store.enqueue_message,
                session_id,
                worker_id,
                message,
                message_id=message_id,
            )
        except Exception as exc:
            return self._dw_error_response(exc)
        return web.json_response(
            {
                "object": "hermes.durable_worker_message",
                "session_id": session_id,
                "message": queued,
            },
            status=201 if queued.get("created") else 200,
        )

    async def _handle_dw_activations(self, request: "web.Request") -> "web.Response":
        session_id, _session, err = await self._dw_session_or_error(request)
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
        session_id, _session, err = await self._dw_session_or_error(request)
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

    async def _handle_dw_create_task(self, request: "web.Request") -> "web.Response":
        session_id, _session, err = await self._dw_session_or_error(request)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        if not isinstance(body, dict):
            return web.json_response(
                _openai_error("Request body must be a JSON object"), status=400
            )
        unknown = self._dw_unknown_fields(body, {"subject", "description", "worker_id"})
        if unknown:
            return unknown
        try:
            worker_id = self._dw_identifier(
                body.get("worker_id"), field="worker_id", required=False
            )
            store = await asyncio.to_thread(self._durable_worker_store)
            task = await asyncio.to_thread(
                store.create_task,
                session_id,
                subject=str(body.get("subject") or ""),
                description=str(body.get("description") or ""),
                worker_id=worker_id,
            )
        except Exception as exc:
            return self._dw_error_response(exc)
        return web.json_response(
            {
                "object": "hermes.durable_worker_task",
                "session_id": session_id,
                "task": task,
            },
            status=201,
        )

    async def _handle_dw_update_task(self, request: "web.Request") -> "web.Response":
        session_id, _session, err = await self._dw_session_or_error(request)
        if err:
            return err
        task_id = str(request.match_info.get("task_id") or "").strip()
        body, err = await self._read_json_body(request)
        if err:
            return err
        if not isinstance(body, dict):
            return web.json_response(
                _openai_error("Request body must be a JSON object"), status=400
            )
        unknown = self._dw_unknown_fields(body, {"status", "expected_revision"})
        if unknown:
            return unknown
        try:
            status = str(body.get("status") or "").strip()
            revision = body.get("expected_revision")
            if revision is not None and (
                isinstance(revision, bool) or not isinstance(revision, int)
            ):
                raise DurableWorkerError(
                    "expected_revision must be an integer or null"
                )
            store = await asyncio.to_thread(self._durable_worker_store)
            task = await asyncio.to_thread(
                store.update_task,
                session_id,
                task_id,
                status=status,
                expected_revision=revision,
            )
        except Exception as exc:
            return self._dw_error_response(exc)
        return web.json_response(
            {
                "object": "hermes.durable_worker_task",
                "session_id": session_id,
                "task": task,
            }
        )

    async def _handle_dw_add_dependency(self, request: "web.Request") -> "web.Response":
        session_id, _session, err = await self._dw_session_or_error(request)
        if err:
            return err
        task_id = str(request.match_info.get("task_id") or "").strip()
        body, err = await self._read_json_body(request)
        if err:
            return err
        if not isinstance(body, dict):
            return web.json_response(
                _openai_error("Request body must be a JSON object"), status=400
            )
        unknown = self._dw_unknown_fields(body, {"blocked_by_task_id"})
        if unknown:
            return unknown
        try:
            blocked_by = self._dw_identifier(
                body.get("blocked_by_task_id"),
                field="blocked_by_task_id",
                required=True,
            )
            store = await asyncio.to_thread(self._durable_worker_store)
            task = await asyncio.to_thread(
                store.add_task_dependency,
                session_id,
                task_id,
                blocked_by,
            )
        except Exception as exc:
            return self._dw_error_response(exc)
        return web.json_response(
            {
                "object": "hermes.durable_worker_task",
                "session_id": session_id,
                "task": task,
            }
        )

    def _dw_runtime_request(
        self, session: dict[str, Any]
    ) -> tuple[dict[str, Any], Any, Any]:
        runtime_request = self._effective_session_runtime_request(
            session=session, body={}
        )
        stored_model = self._stored_session_model(session)
        stored_route = self._resolve_route(stored_model) if stored_model else None
        route = runtime_request.get("route") or stored_route
        session_model = stored_model if (stored_model and stored_route is None) else None
        return runtime_request, route, session_model

    def _dw_execute_reserved_sync(
        self,
        *,
        request_profile: Optional[str],
        session_id: str,
        worker_id: str,
        reserved: dict[str, Any],
        session: dict[str, Any],
    ) -> dict[str, Any]:
        from agent.subagent_lifecycle import SubagentLifecycleService

        with self._profile_scope(request_profile):
            runtime_request, route, session_model = self._dw_runtime_request(session)
            requested = runtime_request.get("requested") or {}
            parent = self._create_agent(
                session_id=session_id,
                route=route,
                session_model=session_model,
                requested_runtime=requested,
                route_source=runtime_request.get("route_source") or "global",
                confirmed_runtime_lock=bool(
                    runtime_request.get("require_model_lock")
                ),
            )
            lifecycle = SubagentLifecycleService(lambda: parent)
            store = self._durable_worker_store()
            return execute_reserved_activation(
                store, lifecycle, lambda: parent, worker_id, reserved
            )

    async def _dw_execute_reserved_background(self, **kwargs: Any) -> None:
        loop = asyncio.get_running_loop()
        activation_id = str(kwargs["reserved"].get("activation_id") or "")
        try:
            await loop.run_in_executor(
                _DW_EXECUTOR,
                lambda: self._dw_execute_reserved_sync(**kwargs),
            )
        except Exception as exc:
            logger.warning(
                "Durable Worker activation %s failed in API background execution: %s",
                activation_id,
                type(exc).__name__,
            )

    def _dw_activation_done(self, task: asyncio.Task) -> None:
        self._dw_activation_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Durable Worker background task failed", exc_info=True)

    async def _handle_dw_run(self, request: "web.Request") -> "web.Response":
        session_id, session, err = await self._dw_session_or_error(request)
        if err:
            return err
        worker_id = str(request.match_info.get("worker_id") or "").strip()

        runtime_request, _route, _session_model = self._dw_runtime_request(session)
        lock_error = self._runtime_lock_error(runtime_request)
        if lock_error is not None:
            return lock_error

        if self._dw_dispatch_lock is None:
            self._dw_dispatch_lock = asyncio.Lock()
        async with self._dw_dispatch_lock:
            self._dw_activation_tasks = {
                task for task in self._dw_activation_tasks if not task.done()
            }
            if (
                len(self._dw_activation_tasks)
                >= self._dw_max_concurrent_activations
            ):
                return web.json_response(
                    _openai_error(
                        "Durable Worker activation capacity reached; retry shortly.",
                        code="durable_worker_capacity",
                    ),
                    status=429,
                    headers={"Retry-After": "1"},
                )
            try:
                store = await asyncio.to_thread(self._durable_worker_store)
                reserved = await asyncio.to_thread(
                    store.reserve_next_activation, session_id, worker_id
                )
            except Exception as exc:
                return self._dw_error_response(exc)
            if reserved.get("status") != "RESERVED":
                return web.json_response(
                    _openai_error(
                        f"Durable Worker cannot start: {reserved.get('status', 'UNKNOWN')}",
                        code=str(
                            reserved.get("status") or "durable_worker_not_ready"
                        ).lower(),
                    ),
                    status=409,
                )

            task = asyncio.create_task(
                self._dw_execute_reserved_background(
                    request_profile=_api_request_profile.get(),
                    session_id=session_id,
                    worker_id=worker_id,
                    reserved=reserved,
                    session=session,
                )
            )
            self._dw_activation_tasks.add(task)
            task.add_done_callback(self._dw_activation_done)

        message = reserved["message"]
        return web.json_response(
            {
                "object": "hermes.durable_worker_activation",
                "session_id": session_id,
                "worker_id": worker_id,
                "activation_id": reserved["activation_id"],
                "message_id": message["message_id"],
                "status": "STARTING",
            },
            status=202,
        )

    async def _handle_dw_events(
        self, request: "web.Request"
    ) -> "web.StreamResponse":
        session_id, _session, err = await self._dw_session_or_error(request)
        if err:
            return err
        last_event_id = str(request.headers.get("Last-Event-ID") or "").strip()
        if last_event_id and not re.fullmatch(
            r"(?:empty|[0-9a-f]{64})", last_event_id
        ):
            return web.json_response(
                _openai_error(
                    "Invalid Last-Event-ID", code="invalid_event_cursor"
                ),
                status=400,
            )
        if self._dw_event_subscribers >= _DW_MAX_EVENT_SUBSCRIBERS:
            return web.json_response(
                _openai_error(
                    "Durable Worker event subscriber capacity reached.",
                    code="durable_worker_event_capacity",
                ),
                status=429,
                headers={"Retry-After": "1"},
            )

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Hermes-Session-Id": session_id,
            },
        )
        self._dw_event_subscribers += 1
        try:
            await response.prepare(request)
            last_token = last_event_id
            last_keepalive = time.monotonic()
            while True:
                db_path = self._durable_worker_db_path()
                if not db_path.exists():
                    token = "empty"
                else:
                    try:
                        token = await asyncio.to_thread(
                            self._durable_worker_projection().change_token,
                            session_id,
                        )
                    except NotFoundError:
                        token = "empty"
                if token != last_token:
                    payload = {
                        "event": "durable_workers.changed",
                        "session_id": session_id,
                        "change_token": token,
                        "timestamp": time.time(),
                    }
                    await response.write(
                        f"id: {token}\n".encode("ascii")
                        + _sse_frame(
                            payload, event="durable_workers.changed"
                        )
                    )
                    last_token = token
                now = time.monotonic()
                if now - last_keepalive >= _DW_SSE_KEEPALIVE_SECONDS:
                    await response.write(b": keepalive\n\n")
                    last_keepalive = now
                await asyncio.sleep(_DW_SSE_POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except (
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            OSError,
        ):
            pass
        finally:
            self._dw_event_subscribers = max(
                0, self._dw_event_subscribers - 1
            )
        return response
