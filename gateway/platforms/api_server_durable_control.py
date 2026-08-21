"""H4 operator controls for the authenticated Durable Workers API.

This adapter extends the qualified H2.1 runtime adapter with a deliberately
small control surface: session-scoped operational counts, retry of a terminal
failed worker, and cancellation of a currently supervised activation.
"""
from __future__ import annotations

from typing import Any, Optional

from agent.durable_worker_control import DurableWorkerControl
from agent.durable_workers import DurableWorkerError
from gateway.platforms.api_server import _openai_error, web
from gateway.platforms.api_server_durable_runtime import (
    DurableWorkersRuntimeAPIServerAdapter,
)


class DurableWorkersControlAPIServerAdapter(DurableWorkersRuntimeAPIServerAdapter):
    """H4 runtime adapter with bounded operator recovery/control routes."""

    _DURABLE_WORKER_CONTROL_ROUTES = (
        (
            "GET",
            "/api/sessions/{session_id}/worker-operations",
            "_handle_dw_operations",
        ),
        (
            "POST",
            "/api/sessions/{session_id}/workers/{worker_id}/retry",
            "_handle_dw_retry_worker",
        ),
        (
            "POST",
            "/api/sessions/{session_id}/workers/{worker_id}/activations/{activation_id}/cancel",
            "_handle_dw_cancel_activation",
        ),
    )

    def _http_route_table(self) -> list[tuple]:
        routes = list(super()._http_route_table())
        routes.extend(
            (method, path, getattr(self, handler_name))
            for method, path, handler_name in self._DURABLE_WORKER_CONTROL_ROUTES
        )
        return routes

    def _durable_worker_control(self) -> DurableWorkerControl:
        return DurableWorkerControl(self._durable_worker_store())

    async def _handle_dw_operations(self, request: "web.Request") -> "web.Response":
        session_id, _session, err = await self._dw_session_or_error(request)
        if err:
            return err
        try:
            summary = self._durable_worker_control().session_summary(session_id)
        except Exception as exc:
            return self._dw_error_response(exc)
        return web.json_response(
            {
                "object": "hermes.durable_worker.operations",
                "session_id": session_id,
                "configured_max_concurrent_activations": self._dw_max_concurrent_activations,
                **summary,
            }
        )

    async def _handle_dw_retry_worker(self, request: "web.Request") -> "web.Response":
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
        expected_revision: Optional[int] = body.get("expected_revision")
        if expected_revision is not None and type(expected_revision) is not int:
            return web.json_response(
                _openai_error(
                    "expected_revision must be an integer or null",
                    code="invalid_durable_worker_request",
                ),
                status=400,
            )
        try:
            result = self._durable_worker_control().retry_failed_worker(
                session_id,
                worker_id,
                expected_revision=expected_revision,
            )
        except Exception as exc:
            return self._dw_error_response(exc)
        return web.json_response(
            {
                "object": "hermes.durable_worker.retry",
                "session_id": session_id,
                **result,
            }
        )

    async def _handle_dw_cancel_activation(
        self, request: "web.Request"
    ) -> "web.Response":
        session_id, _session, err = await self._dw_session_or_error(request)
        if err:
            return err
        worker_id = str(request.match_info.get("worker_id") or "").strip()
        activation_id = str(request.match_info.get("activation_id") or "").strip()
        body, err = await self._read_json_body(request)
        if err:
            return err
        if not isinstance(body, dict):
            return web.json_response(
                _openai_error("Request body must be a JSON object"), status=400
            )
        unknown = self._dw_unknown_fields(body, {"reason"})
        if unknown:
            return unknown
        reason_raw: Any = body.get("reason")
        if reason_raw is None:
            reason = "operator requested durable worker cancellation"
        elif not isinstance(reason_raw, str):
            return web.json_response(
                _openai_error(
                    "reason must be a string or null",
                    code="invalid_durable_worker_request",
                ),
                status=400,
            )
        else:
            reason = reason_raw.strip() or "operator requested durable worker cancellation"
        if len(reason) > 500 or any(ch in reason for ch in ("\r", "\n", "\x00")):
            return web.json_response(
                _openai_error(
                    "reason must contain at most 500 safe characters",
                    code="invalid_durable_worker_request",
                ),
                status=400,
            )

        control = self._durable_worker_control()
        try:
            activation = control.get_activation(session_id, worker_id, activation_id)
        except Exception as exc:
            return self._dw_error_response(exc)

        state = str(activation.get("state") or "")
        if state == "CANCEL_REQUESTED":
            return web.json_response(
                {
                    "object": "hermes.durable_worker.activation.cancel",
                    "session_id": session_id,
                    "worker_id": worker_id,
                    "activation_id": activation_id,
                    "status": "CANCEL_REQUESTED",
                    "accepted": True,
                    "already_requested": True,
                },
                status=202,
            )
        if state not in {"STARTING", "RUNNING"}:
            return web.json_response(
                _openai_error(
                    f"Activation cannot be cancelled from state {state or 'UNKNOWN'}.",
                    code="durable_worker_activation_not_cancelable",
                ),
                status=409,
            )

        # A durable row alone is not proof that this process controls the child.
        # Only the transient supervision map contains a capability-bearing live
        # handle that the public lifecycle service can safely cancel.
        with self._dw_active_lock:
            active = self._dw_active_lifecycles.get(activation_id)
        if active is None:
            return web.json_response(
                _openai_error(
                    "Activation is not supervised by this API process; cancellation was not attempted.",
                    code="durable_worker_activation_not_locally_supervised",
                ),
                status=409,
            )
        lifecycle, handle = active
        if str(getattr(handle, "subagent_id", "") or "") != str(
            activation.get("subagent_id") or ""
        ):
            return web.json_response(
                _openai_error(
                    "Activation supervision handle does not match durable state.",
                    code="durable_worker_activation_supervision_mismatch",
                ),
                status=409,
            )

        try:
            requested = control.request_cancel(session_id, worker_id, activation_id)
        except Exception as exc:
            return self._dw_error_response(exc)
        previous_state = str(requested.get("previous_state") or state)

        try:
            result = lifecycle.cancel(handle, reason=reason)
        except Exception:
            control.restore_cancel_request(
                session_id,
                worker_id,
                activation_id,
                previous_state=previous_state,
            )
            return web.json_response(
                _openai_error(
                    "Lifecycle cancellation failed before acceptance.",
                    code="durable_worker_cancellation_unavailable",
                ),
                status=503,
            )

        if getattr(result, "accepted", False):
            return web.json_response(
                {
                    "object": "hermes.durable_worker.activation.cancel",
                    "session_id": session_id,
                    "worker_id": worker_id,
                    "activation_id": activation_id,
                    "subagent_id": str(getattr(handle, "subagent_id", "") or ""),
                    "status": "CANCEL_REQUESTED",
                    "accepted": True,
                    "already_requested": False,
                },
                status=202,
            )

        # A child may become terminal between the durable marker and cancel().
        # Do not roll back in that race: the background executor owns terminal
        # reconciliation and will resolve SUCCEEDED/FAILED/CANCELLED correctly.
        if getattr(result, "already_terminal", False):
            return web.json_response(
                {
                    "object": "hermes.durable_worker.activation.cancel",
                    "session_id": session_id,
                    "worker_id": worker_id,
                    "activation_id": activation_id,
                    "status": "TERMINAL_RECONCILIATION_PENDING",
                    "accepted": False,
                    "already_terminal": True,
                },
                status=202,
            )

        control.restore_cancel_request(
            session_id,
            worker_id,
            activation_id,
            previous_state=previous_state,
        )
        return web.json_response(
            _openai_error(
                "Lifecycle cancellation was not accepted.",
                code="durable_worker_cancellation_unavailable",
            ),
            status=409,
        )


__all__ = ["DurableWorkersControlAPIServerAdapter"]
