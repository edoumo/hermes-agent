"""Registry discovery tests for the governed_mkfs tool.

The registry's AST-based discovery only imports modules with a top-level
``registry.register(...)`` call.  If that invariant is ever broken, the
governed path silently disappears from the model's toolset — which would
be a safety regression (the model would have no governed alternative and
could be tempted to bypass).  These tests pin the discovery contract.
"""

from pathlib import Path

from tools.registry import _module_registers_tools, registry


def test_governed_mkfs_module_is_ast_discovered():
    module_path = Path("tools/governed_mkfs_tool.py")
    assert module_path.exists()
    assert _module_registers_tools(module_path) is True


def test_governed_mkfs_registered_after_import():
    import tools.governed_mkfs_tool  # noqa: F401

    entry = registry.get_entry("governed_mkfs")
    assert entry is not None
    assert entry.name == "governed_mkfs"
    assert entry.toolset == "terminal"
    # The schema must expose only structured fields, never a free shell string.
    props = entry.schema["parameters"]["properties"]
    assert set(props) == {"grant_id", "vm_id", "device", "fs_type", "label"}
    assert entry.schema["parameters"]["required"] == [
        "grant_id", "vm_id", "device", "fs_type", "label",
    ]


def test_governed_mkfs_handler_never_issues_grants():
    """The handler can verify/consume but structurally cannot issue."""
    import inspect

    import tools.governed_mkfs_tool as gmt

    src = inspect.getsource(gmt._handle_governed_mkfs)
    assert "issue_grant" not in src
    assert "authorization_subject" not in src
