"""H6 platform-factory tests for the opt-in Durable Workers API server."""

from types import SimpleNamespace

import plugins.platforms.api_server as plugin


def test_factory_keeps_stock_api_server_by_default(monkeypatch):
    monkeypatch.setattr(plugin, "APIServerAdapter", lambda cfg: ("stock", cfg))
    monkeypatch.setattr(
        plugin,
        "DurableWorkersFinalAPIServerAdapter",
        lambda cfg: ("durable", cfg),
    )
    cfg = SimpleNamespace(extra={})
    assert plugin._factory(cfg)[0] == "stock"


def test_factory_requires_explicit_h6_opt_in(monkeypatch):
    monkeypatch.setattr(plugin, "APIServerAdapter", lambda cfg: ("stock", cfg))
    monkeypatch.setattr(
        plugin,
        "DurableWorkersFinalAPIServerAdapter",
        lambda cfg: ("durable", cfg),
    )
    for enabled in (True, "true", "1", "yes", "on", 1):
        cfg = SimpleNamespace(extra={"durable_workers_api": enabled})
        assert plugin._factory(cfg)[0] == "durable"
    for disabled in (False, None, "false", "0", "off", 0):
        cfg = SimpleNamespace(extra={"durable_workers_api": disabled})
        assert plugin._factory(cfg)[0] == "stock"


def test_register_overrides_only_api_server_platform():
    calls = []

    class Ctx:
        def register_platform(self, **kwargs):
            calls.append(kwargs)

    plugin.register(Ctx())
    assert len(calls) == 1
    assert calls[0]["name"] == "api_server"
    assert calls[0]["adapter_factory"] is plugin._factory
