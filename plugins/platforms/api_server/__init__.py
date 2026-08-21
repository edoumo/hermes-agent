"""Opt-in H5 extension of Hermes' built-in API server platform."""

from __future__ import annotations

from typing import Any

from gateway.platforms.api_server import AIOHTTP_AVAILABLE, APIServerAdapter
from gateway.platforms.api_server_durable_task_recovery import (
    DurableWorkersTaskRecoveryAPIServerAdapter,
)


def _enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _factory(config):
    """Return stock API server unless Durable Workers API is explicitly opted in."""
    extra = getattr(config, "extra", {}) or {}
    if _enabled(extra.get("durable_workers_api")):
        return DurableWorkersTaskRecoveryAPIServerAdapter(config)
    return APIServerAdapter(config)


def register(ctx) -> None:
    """Override the built-in factory without changing default behavior."""
    ctx.register_platform(
        name="api_server",
        label="API Server",
        adapter_factory=_factory,
        check_fn=lambda: bool(AIOHTTP_AVAILABLE),
        install_hint="aiohttp is required by the Hermes API server",
        pii_safe=True,
        allow_update_command=False,
    )


__all__ = ["register"]
