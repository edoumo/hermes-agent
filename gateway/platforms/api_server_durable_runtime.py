"""Runtime glue for H2.1 Durable Workers API activations.

The read/write/SSE surface lives in :mod:`gateway.platforms.api_server_durable`.
This subclass owns only the bridge from a pre-reserved durable activation to
Hermes' native ``APIServerAdapter._create_agent`` contract.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from agent.durable_worker_execution import execute_reserved_activation
from gateway.platforms.api_server_durable import DurableWorkersAPIServerAdapter

logger = logging.getLogger(__name__)


class DurableWorkersRuntimeAPIServerAdapter(DurableWorkersAPIServerAdapter):
    """H2.1 adapter with the native API-server runtime bridge."""

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

        activation_id = str(reserved.get("activation_id") or "")
        message = reserved.get("message") or {}
        message_id = str(message.get("message_id") or "")
        store = self._durable_worker_store()

        with self._profile_scope(request_profile):
            try:
                runtime_request, route, session_model = self._dw_runtime_request(session)
                requested = runtime_request.get("requested") or {}
                parent = self._create_agent(
                    session_id=session_id,
                    requested_model=requested.get("model"),
                    requested_provider=requested.get("provider"),
                    model_options=runtime_request.get("model_options") or {},
                    route=route,
                    session_model=session_model,
                    confirmed_runtime_lock=bool(
                        runtime_request.get("require_model_lock")
                    ),
                )
                lifecycle = SubagentLifecycleService(lambda: parent)
            except Exception as exc:
                # The reservation already moved the durable state to
                # STARTING/PROCESSING/RUNNING. If parent-agent construction
                # fails before execute_reserved_activation can own cleanup,
                # restore that reservation immediately instead of waiting for
                # a process restart to recover it as ABANDONED.
                if activation_id and message_id:
                    try:
                        store.finish_activation(
                            session_id,
                            worker_id,
                            activation_id,
                            message_id,
                            state="FAILED_TO_START",
                            error=str(exc)[:32000],
                            message_state="PENDING",
                            worker_state="DORMANT",
                        )
                    except Exception:
                        logger.exception(
                            "Failed restoring Durable Worker reservation after "
                            "API parent-agent construction failure"
                        )
                raise

            def _started(active_lifecycle: Any, handle: Any) -> None:
                with self._dw_active_lock:
                    self._dw_active_lifecycles[activation_id] = (
                        active_lifecycle,
                        handle,
                    )

            try:
                return execute_reserved_activation(
                    store,
                    lifecycle,
                    lambda: parent,
                    worker_id,
                    reserved,
                    on_started=_started,
                )
            finally:
                with self._dw_active_lock:
                    self._dw_active_lifecycles.pop(activation_id, None)


__all__ = ["DurableWorkersRuntimeAPIServerAdapter"]
