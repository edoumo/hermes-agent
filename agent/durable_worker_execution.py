"""Execution seam for pre-reserved Hermes Durable Worker activations.

H2 needs to reserve an activation before returning HTTP 202 so the client gets
its durable activation id immediately. This module executes that already
reserved activation through Hermes' public SubagentLifecycleService without
introducing a second agent loop or serializing live agent objects.
"""
from __future__ import annotations

from typing import Any, Callable

from agent.durable_workers import (
    DurableWorkerAuthorizationError,
    DurableWorkerConflictError,
)


def _parent_session_id(parent_resolver: Callable[[], Any]) -> str:
    parent = parent_resolver()
    session_id = str(getattr(parent, "session_id", "") or "").strip()
    if not session_id:
        raise DurableWorkerAuthorizationError(
            "Durable workers require an active Hermes parent session."
        )
    return session_id


def execute_reserved_activation(
    store: Any,
    lifecycle: Any,
    parent_resolver: Callable[[], Any],
    worker_id: str,
    reserved: dict[str, Any],
) -> dict[str, Any]:
    """Execute one activation previously returned by reserve_next_activation.

    Reservation remains the cross-process serialization boundary. This helper
    never reserves a second message and never accepts a per-launch timeout,
    because Hermes' public lifecycle contract explicitly rejects launch-level
    timeouts. Runtime timeout policy stays owned by Hermes delegation config.
    """
    parent = _parent_session_id(parent_resolver)
    if reserved.get("status") != "RESERVED":
        raise DurableWorkerConflictError("activation reservation is not executable")
    if str(reserved.get("worker_id") or "") != worker_id:
        raise DurableWorkerConflictError("activation reservation worker mismatch")

    activation_id = str(reserved.get("activation_id") or "").strip()
    message = reserved.get("message")
    if not activation_id or not isinstance(message, dict):
        raise DurableWorkerConflictError("activation reservation is incomplete")
    if str(message.get("worker_id") or "") != worker_id:
        raise DurableWorkerConflictError("activation message worker mismatch")
    message_id = str(message.get("message_id") or "").strip()
    if not message_id or message.get("state") != "PROCESSING":
        raise DurableWorkerConflictError("activation message is not processing")

    worker = store.get_worker(parent, worker_id)
    context = store.render_context(
        parent, worker_id, exclude_message_id=message_id
    )

    from agent.subagent_lifecycle import SubagentLaunchRequest

    try:
        # Do not pass timeout_seconds here. SubagentLifecycleService validates
        # that launch-level timeouts are unsupported; hard child limits belong
        # to Hermes delegation.child_timeout_seconds instead.
        handle = lifecycle.launch(
            SubagentLaunchRequest(
                goal=message["content"],
                context=context,
                role=worker["role"],
                model=worker["model"],
                allowed_toolsets=tuple(worker["toolsets"]) or None,
                parent_session_id=parent,
                correlation_id=activation_id,
                metadata={
                    "durable_worker_id": worker_id,
                    "durable_activation_id": activation_id,
                },
            )
        )
    except Exception as exc:
        store.finish_activation(
            parent,
            worker_id,
            activation_id,
            message_id,
            state="FAILED_TO_START",
            error=str(exc)[:32000],
            message_state="PENDING",
            worker_state="DORMANT",
        )
        raise

    try:
        store.bind_activation(activation_id, handle.subagent_id)
    except Exception:
        try:
            lifecycle.cancel(handle, reason="durable activation bind failed")
        finally:
            store.mark_cancel_requested(parent, worker_id, activation_id)
        raise

    terminal = lifecycle.wait(handle)
    if not terminal.completed:
        try:
            lifecycle.cancel(
                handle, reason="durable activation did not reach terminal state"
            )
        finally:
            store.mark_cancel_requested(parent, worker_id, activation_id)
        return {
            "worker_id": worker_id,
            "activation_id": activation_id,
            "subagent_id": handle.subagent_id,
            "status": "CANCEL_REQUESTED",
        }

    result = lifecycle.result(handle)
    state = getattr(terminal.state, "value", str(terminal.state))
    summary = getattr(result, "summary", None)
    error = getattr(result, "error_message", None)
    if state == "SUCCEEDED" and getattr(result, "ready", False):
        text = str(summary or "(completed without summary)")[:32000]
        report = store.finish_activation(
            parent,
            worker_id,
            activation_id,
            message_id,
            state="SUCCEEDED",
            summary=text,
            message_state="CONSUMED",
            worker_state="DORMANT",
        )
        return {
            "worker_id": worker_id,
            "activation_id": activation_id,
            "subagent_id": handle.subagent_id,
            "status": "SUCCEEDED",
            "summary": text,
            "report_message_id": report["message_id"] if report else None,
        }

    error_text = str(error or state or "activation failed")[:32000]
    store.finish_activation(
        parent,
        worker_id,
        activation_id,
        message_id,
        state=state or "FAILED",
        error=error_text,
        message_state="FAILED",
        worker_state="FAILED",
    )
    return {
        "worker_id": worker_id,
        "activation_id": activation_id,
        "subagent_id": handle.subagent_id,
        "status": state or "FAILED",
        "error": error_text,
    }
