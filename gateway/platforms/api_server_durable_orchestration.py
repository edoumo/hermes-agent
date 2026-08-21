"""H5 task-orchestration extension for the authenticated Hermes API server.

This adapter layers on the qualified H4 control adapter.  It adds DAG editing,
read-only graph projection and atomic dispatch of READY tasks into the existing
Durable Worker activation lifecycle.  It does not add a listener, auth path or
second execution engine.
"""
from __future__ import annotations

import asyncio
from typing import Any

from agent.durable_task_orchestration import (
    DurableTaskGraphProjection,
    DurableTaskOrchestrator,
)
from agent.durable_workers import DurableWorkerError
from gateway.platforms.api_server import _api_request_profile, _openai_error, web
from gateway.platforms.api_server_durable_control import (
    DurableWorkersControlAPIServerAdapter,
)


class DurableWorkersTaskOrchestrationAPIServerAdapter(
    DurableWorkersControlAPIServerAdapter
):
    """H5 adapter with bounded task-DAG editing and real task dispatch."""

    _DURABLE_TASK_ORCHESTRATION_ROUTES = (
        (
            "GET",
            "/api/sessions/{session_id}/worker-task-graph",
            "_handle_dw_task_graph",
        ),
        (
            "POST",
            "/api/sessions/{session_id}/worker-tasks/{task_id}/edit",
            "_handle_dw_task_edit",
        ),
        (
            "POST",
            "/api/sessions/{session_id}/worker-tasks/{task_id}/dependencies/add",
            "_handle_dw_task_dependency_add_h5",
        ),
        (
            "POST",
            "/api/sessions/{session_id}/worker-tasks/{task_id}/dependencies/remove",
            "_handle_dw_task_dependency_remove_h5",
        ),
        (
            "POST",
            "/api/sessions/{session_id}/worker-tasks/{task_id}/dispatch",
            "_handle_dw_task_dispatch",
        ),
    )

    def _http_route_table(self) -> list[tuple]:
        routes = list(super()._http_route_table())
        routes.extend(
            (method, path, getattr(self, handler_name))
            for method, path, handler_name in self._DURABLE_TASK_ORCHESTRATION_ROUTES
        )
        return routes

    def _durable_task_graph_projection(self) -> DurableTaskGraphProjection:
        return DurableTaskGraphProjection(self._durable_worker_db_path())

    async def _dw_h5_owned_task_or_error(
        self, session_id: str, task_id: str
    ) -> tuple[dict[str, Any] | None, "web.Response" | None]:
        try:
            task = await asyncio.to_thread(
                self._durable_task_graph_projection().get_task,
                session_id,
                task_id,
            )
            return task, None
        except Exception as exc:
            return None, self._dw_error_response(exc)

    @staticmethod
    def _dw_h5_expected_revision(body: dict[str, Any]) -> int:
        value = body.get("expected_revision")
        if isinstance(value, bool) or not isinstance(value, int):
            raise DurableWorkerError("expected_revision must be an integer")
        return value

    async def _handle_dw_task_graph(self, request: "web.Request") -> "web.Response":
        session_id, _session, err = await self._dw_session_or_error(request)
        if err:
            return err
        unknown = sorted(set(request.query.keys()) - {"limit"})
        if unknown:
            return web.json_response(
                _openai_error(
                    "Unsupported task graph query parameters: " + ", ".join(unknown),
                    code="invalid_durable_worker_request",
                ),
                status=400,
            )
        raw_limit = request.query.get("limit")
        try:
            limit = 100 if raw_limit is None else int(raw_limit)
            if isinstance(raw_limit, bool) or limit < 1 or limit > 100:
                raise ValueError
        except (TypeError, ValueError):
            return web.json_response(
                _openai_error(
                    "limit must be an integer between 1 and 100",
                    code="invalid_durable_worker_request",
                ),
                status=400,
            )
        if not self._durable_worker_db_path().exists():
            graph = {
                "tasks": [],
                "edges": [],
                "counts": {
                    "total": 0,
                    "pending": 0,
                    "ready": 0,
                    "blocked": 0,
                    "in_progress": 0,
                    "completed": 0,
                    "failed": 0,
                    "cancelled": 0,
                },
                "truncated": False,
            }
        else:
            try:
                graph = await asyncio.to_thread(
                    self._durable_task_graph_projection().graph,
                    session_id,
                    limit=limit,
                )
            except Exception as exc:
                return self._dw_error_response(exc)
        return web.json_response(
            {
                "object": "hermes.durable_worker_task.graph",
                "session_id": session_id,
                **graph,
            }
        )

    async def _handle_dw_task_edit(self, request: "web.Request") -> "web.Response":
        session_id, _session, err = await self._dw_session_or_error(request)
        if err:
            return err
        task_id = str(request.match_info.get("task_id") or "").strip()
        _task, ownership_error = await self._dw_h5_owned_task_or_error(session_id, task_id)
        if ownership_error:
            return ownership_error
        query_error = self._dw_control_query_error(request)
        if query_error:
            return query_error
        body, err = await self._read_json_body(request)
        if err:
            return err
        unknown = self._dw_unknown_fields(
            body,
            {"subject", "description", "worker_id", "expected_revision"},
        )
        if unknown:
            return unknown
        try:
            revision = self._dw_h5_expected_revision(body)
            worker_present = "worker_id" in body
            worker_id = None
            if worker_present and body.get("worker_id") is not None:
                worker_id = self._dw_identifier(
                    body.get("worker_id"), field="worker_id", required=False
                )
            store = await asyncio.to_thread(self._durable_worker_store)
            orchestrator = await asyncio.to_thread(DurableTaskOrchestrator, store)
            task = await asyncio.to_thread(
                orchestrator.edit_task,
                session_id,
                task_id,
                expected_revision=revision,
                subject=body.get("subject") if "subject" in body else None,
                description=body.get("description") if "description" in body else None,
                worker_id=worker_id,
                worker_id_present=worker_present,
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

    async def _dw_h5_dependency_change(
        self, request: "web.Request", *, remove: bool
    ) -> "web.Response":
        session_id, _session, err = await self._dw_session_or_error(request)
        if err:
            return err
        task_id = str(request.match_info.get("task_id") or "").strip()
        _task, ownership_error = await self._dw_h5_owned_task_or_error(session_id, task_id)
        if ownership_error:
            return ownership_error
        query_error = self._dw_control_query_error(request)
        if query_error:
            return query_error
        body, err = await self._read_json_body(request)
        if err:
            return err
        unknown = self._dw_unknown_fields(
            body, {"blocked_by_task_id", "expected_revision"}
        )
        if unknown:
            return unknown
        try:
            revision = self._dw_h5_expected_revision(body)
            blocked_by = self._dw_identifier(
                body.get("blocked_by_task_id"),
                field="blocked_by_task_id",
                required=True,
            )
            store = await asyncio.to_thread(self._durable_worker_store)
            orchestrator = await asyncio.to_thread(DurableTaskOrchestrator, store)
            operation = (
                orchestrator.remove_dependency if remove else orchestrator.add_dependency
            )
            task = await asyncio.to_thread(
                operation,
                session_id,
                task_id,
                blocked_by,
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

    async def _handle_dw_task_dependency_add_h5(
        self, request: "web.Request"
    ) -> "web.Response":
        return await self._dw_h5_dependency_change(request, remove=False)

    async def _handle_dw_task_dependency_remove_h5(
        self, request: "web.Request"
    ) -> "web.Response":
        return await self._dw_h5_dependency_change(request, remove=True)

    def _dw_execute_reserved_sync(self, **kwargs: Any) -> dict[str, Any]:
        reserved = kwargs.get("reserved") or {}
        task_id = str(reserved.get("task_id") or "").strip()
        activation_id = str(reserved.get("activation_id") or "").strip()
        session_id = str(kwargs.get("session_id") or "").strip()
        if not task_id:
            return super()._dw_execute_reserved_sync(**kwargs)
        try:
            result = super()._dw_execute_reserved_sync(**kwargs)
        except Exception as exc:
            try:
                store = self._durable_worker_store()
                DurableTaskOrchestrator(store).reconcile_exception(
                    session_id, task_id, activation_id, exc
                )
            except Exception:
                pass
            raise
        store = self._durable_worker_store()
        DurableTaskOrchestrator(store).reconcile_result(
            session_id, task_id, activation_id, result
        )
        result = dict(result)
        result["task_id"] = task_id
        return result

    async def _handle_dw_task_dispatch(
        self, request: "web.Request"
    ) -> "web.Response":
        session_id, session, err = await self._dw_session_or_error(request)
        if err:
            return err
        task_id = str(request.match_info.get("task_id") or "").strip()
        _task, ownership_error = await self._dw_h5_owned_task_or_error(session_id, task_id)
        if ownership_error:
            return ownership_error
        query_error = self._dw_control_query_error(request)
        if query_error:
            return query_error
        body, err = await self._read_json_body(request)
        if err:
            return err
        unknown = self._dw_unknown_fields(body, {"expected_revision"})
        if unknown:
            return unknown
        try:
            revision = self._dw_h5_expected_revision(body)
        except Exception as exc:
            return self._dw_error_response(exc)

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
            if len(self._dw_activation_tasks) >= self._dw_max_concurrent_activations:
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
                orchestrator = await asyncio.to_thread(DurableTaskOrchestrator, store)
                reserved = await asyncio.to_thread(
                    orchestrator.reserve_ready_task,
                    session_id,
                    task_id,
                    expected_revision=revision,
                )
            except Exception as exc:
                return self._dw_error_response(exc)

            task = asyncio.create_task(
                self._dw_execute_reserved_background(
                    request_profile=_api_request_profile.get(),
                    session_id=session_id,
                    worker_id=reserved["worker_id"],
                    reserved=reserved,
                    session=session,
                )
            )
            self._dw_activation_tasks.add(task)
            task.add_done_callback(self._dw_activation_done)

        message = reserved["message"]
        return web.json_response(
            {
                "object": "hermes.durable_worker_task.dispatch",
                "session_id": session_id,
                "task_id": task_id,
                "worker_id": reserved["worker_id"],
                "activation_id": reserved["activation_id"],
                "message_id": message["message_id"],
                "status": "STARTING",
            },
            status=202,
        )


__all__ = ["DurableWorkersTaskOrchestrationAPIServerAdapter"]
