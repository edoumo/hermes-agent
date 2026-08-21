"""H6 contract tests for the opt-in Durable Workers tool plugin."""
from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "durable-workers" / "__init__.py"
METADATA = ROOT / "plugins" / "durable-workers" / "plugin.yaml"

_EXPECTED_ACTIONS = {
    "create",
    "list",
    "show",
    "enqueue",
    "send",
    "run_next",
    "reports",
    "task_create",
    "task_depend",
    "task_update",
    "task_list",
}


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "hermes_test_durable_workers_plugin_h6",
        PLUGIN,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_plugin_preserves_h1_action_surface():
    module = _load_plugin_module()
    actions = set(module._TOOL_SCHEMA["parameters"]["properties"]["action"]["enum"])

    assert actions == _EXPECTED_ACTIONS
    assert module._TOOL_SCHEMA["parameters"]["required"] == ["action"]


def test_tool_plugin_uses_h6_versioned_store():
    source = PLUGIN.read_text(encoding="utf-8")

    assert "from agent.versioned_durable_workers import VersionedDurableWorkerStore" in source
    assert "store = VersionedDurableWorkerStore()" in source
    assert "DurableWorkerStore(" not in source


def test_tool_plugin_metadata_marks_h6_storage_revision():
    metadata = METADATA.read_text(encoding="utf-8")

    assert 'version: "0.2.0"' in metadata
    assert "H6 versioned storage" in metadata
