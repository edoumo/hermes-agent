"""H5 recovery layer for durable task dispatch.

This final H5 API layer adds failed-task recovery and overrides only task
dispatch so a task-owned PENDING message produced by H4 cancel/retry is reused
rather than treated as unrelated inbox backlog.
"""
from __future__ import annotations

import asyncio

from agent.durable_task_recovery import RecoverableDurableTaskOrchestrator
from gateway.platforms.api_server import _api_request_profile, _openai_error, web
from gateway.platforms.api_server_durable_orchestration import (
    DurableWorkersTaskOrchestrationAPIServerAdapter,
)


class DurableWorkersTaskRecoveryAPIServerAdapter(
    DurableWorkersTaskOrchestrationAPIServerAdapter
):
    """H5 orchestration adapter with task-aware recovery/redispatch."""

    _DURABLE_TASK_RECOVERY_ROUTES = (
        (
            "POST",
            "/api/sessions/{session_id}/worker-tasks/{task_id}/recover",
            "_handle_dw_task_recover",
        ),
    )

    def _http_route_table(self) -> list[tuple]:
        routes = list(super()._http_route_table())
        routes.extend(
            (method, path, getattr(self, handler_name))
            for method, path, handler_name in self._DURABLE_TASK_RECOVERY_ROUTES
        )
        return routes

    async def _handle_dw_task_recover(self, request: "web.Request") -> "web.Response":
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
        unknown = self._dw_unknown_fields(body, {"expected_revision"})
        if unknown:
            return unknown
        try:
            revision = self._dw_h5_expected_revision(body)
            store = await asyncio.to_thread(self._durable_worker_store)
            orchestrator = await asyncio.to_thread(
                RecoverableDurableTaskOrchestrator, store
            )
            result = await asyncio.to_thread(
                orchestrator.recover_failed_task,
                session_id,
                task_id,
                expected_revision=revision,
            )
        except Exception as exc:
            return self._dw_error_response(exc)
        return web.json_response(
            {
                "object": "hermes.durable_worker_task.recovery",
                "session_id": session_id,
                **result,
            }
        )

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
                orchestrator = await asyncio.to_thread(
                    RecoverableDurableTaskOrchestrator, store
                )
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
                "reused_message": bool(reserved.get("reused_message")),
            },
            status=202,
        )


__all__ = ["DurableWorkersTaskRecoveryAPIServerAdapter"]
