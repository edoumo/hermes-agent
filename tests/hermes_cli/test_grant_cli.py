"""CLI wiring tests for ``hermes grant`` (trusted user boundary)."""

import argparse
import json
import time
import uuid

import pytest

from hermes_cli.subcommands.grant import (
    _obtain_human_receipt as _REAL_OBTAIN_HUMAN_RECEIPT,
    build_grant_parser,
    run_grant_command,
)

_IDENTITY = {"hostname": "storage-guest", "boot_id": "boot-A", "product_uuid": "uuid-A"}


def _fake_receipt_dict():
    from tools.grant_authority import HumanApprovalReceipt, _store_receipt

    receipt = _store_receipt(
        HumanApprovalReceipt(
            receipt_id="rcpt-" + uuid.uuid4().hex,
            request_id="req-11111111111111111111111111111111",
            request_digest="d" * 64,
            session_id="sess-1",
            turn_id="turn-1",
            tool_call_id="tool-1",
            operation="CREATE_FILESYSTEM",
            vm_id="101",
            device="/dev/sdb1",
            fs_type="ext4",
            label="DATA",
            issued_at=time.time(),
            expires_at=time.time() + 600,
        )
    )
    return receipt.to_dict()


@pytest.fixture(autouse=True)
def _mock_issue_boundaries(monkeypatch):
    """The issue path now requires a live QGA incarnation capture and a
    correlated human approval receipt; both are mocked in CLI tests."""
    from hermes_cli.subcommands import grant as grant_mod

    monkeypatch.setattr(
        "tools.qga_structured.qga_guest_identity",
        lambda vm_id: dict(_IDENTITY),
    )
    monkeypatch.setattr(
        grant_mod, "_obtain_human_receipt", lambda args: _fake_receipt_dict()
    )


def _make_parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_grant_parser(subparsers, cmd_grant=lambda a: run_grant_command(a))
    return parser


class TestGrantParser:
    def test_issue_parses_full_tuple(self):
        parser = _make_parser()
        args = parser.parse_args(
            [
                "grant", "issue",
                "--operation", "CREATE_FILESYSTEM",
                "--vm", "101",
                "--hostname", "storage-guest",
                "--device", "/dev/sdb1",
                "--fs", "ext4",
                "--label", "DATA",
                "--subject", "operator",
                "--session", "sess-1",
                "--ttl", "300",
            ]
        )
        assert args.grant_command == "issue"
        assert args.vm_id == "101"
        assert args.device == "/dev/sdb1"
        assert args.fs_type == "ext4"
        assert args.label == "DATA"
        assert args.subject == "operator"
        assert args.session_id == "sess-1"
        assert args.ttl == 300
        assert hasattr(args, "func")

    def test_list_and_audit_parse(self):
        parser = _make_parser()
        for sub in ("list", "audit"):
            args = parser.parse_args(["grant", sub])
            assert args.grant_command == sub
            assert hasattr(args, "func")

    def test_revoke_parses_grant_id(self):
        parser = _make_parser()
        args = parser.parse_args(["grant", "revoke", "abc-123"])
        assert args.grant_command == "revoke"
        assert args.grant_id == "abc-123"


class TestGrantCommand:
    def test_issue_creates_grant_and_list_shows_it(self, capsys):
        args = _make_parser().parse_args(
            [
                "grant", "issue",
                "--operation", "CREATE_FILESYSTEM",
                "--vm", "101",
                "--hostname", "storage-guest",
                "--device", "/dev/sdb1",
                "--fs", "ext4",
                "--label", "DATA",
                "--subject", "operator",
                "--session", "sess-1",
            ]
        )
        rc = run_grant_command(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "grant_id=" in out
        grant_id = out.split("grant_id=")[1].splitlines()[0].strip()

        # list shows the live grant
        list_args = _make_parser().parse_args(["grant", "list"])
        capsys.readouterr()
        rc = run_grant_command(list_args)
        assert rc == 0
        out = capsys.readouterr().out
        assert grant_id in out
        assert "DATA" in out

        # audit shows the issue event
        audit_args = _make_parser().parse_args(["grant", "audit"])
        capsys.readouterr()
        rc = run_grant_command(audit_args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "grant_issued" in out
        assert grant_id in out

        # revoke removes it
        revoke_args = _make_parser().parse_args(["grant", "revoke", grant_id])
        capsys.readouterr()
        rc = run_grant_command(revoke_args)
        assert rc == 0
        out = capsys.readouterr().out
        assert f"revoked={grant_id}" in out

    def test_issue_rejects_root_device(self, capsys):
        args = _make_parser().parse_args(
            [
                "grant", "issue",
                "--operation", "CREATE_FILESYSTEM",
                "--vm", "101",
                "--hostname", "storage-guest",
                "--device", "/dev/sda1",
                "--fs", "ext4",
                "--label", "X",
                "--subject", "operator",
                "--session", "sess-1",
            ]
        )
        rc = run_grant_command(args)
        assert rc == 2
        err = capsys.readouterr().err
        assert "root/whole-disk" in err

    def test_issue_rejects_hostname_mismatch(self, capsys, monkeypatch):
        monkeypatch.setattr(
            "tools.qga_structured.qga_guest_identity",
            lambda vm_id: {"hostname": "other-host", "boot_id": "boot-A",
                           "product_uuid": "uuid-A"},
        )
        args = _make_parser().parse_args(
            [
                "grant", "issue",
                "--operation", "CREATE_FILESYSTEM",
                "--vm", "101",
                "--hostname", "storage-guest",
                "--device", "/dev/sdb1",
                "--fs", "ext4",
                "--label", "X",
                "--subject", "operator",
                "--session", "sess-1",
            ]
        )
        rc = run_grant_command(args)
        assert rc == 2
        err = capsys.readouterr().err
        assert "does not match" in err

    def test_issue_refuses_unattended_without_transport(self, capsys, monkeypatch):
        """No TTY and no approval transport -> no grant (B1)."""
        from hermes_cli.subcommands import grant as grant_mod

        monkeypatch.setattr(
            "tools.approval._present_with_selected_transport", None
        )
        # Simulate a non-TTY stdin (no isatty, no readline).
        class _NonTtyStdin:
            def isatty(self):
                return False

        monkeypatch.setattr(grant_mod.sys, "stdin", _NonTtyStdin())
        # Restore the REAL receipt gate for this test (the autouse fixture
        # mocks it away for the other tests).
        monkeypatch.setattr(
            grant_mod, "_obtain_human_receipt", _REAL_OBTAIN_HUMAN_RECEIPT
        )
        args = _make_parser().parse_args(
            [
                "grant", "issue",
                "--operation", "CREATE_FILESYSTEM",
                "--vm", "101",
                "--hostname", "storage-guest",
                "--device", "/dev/sdb1",
                "--fs", "ext4",
                "--label", "X",
                "--subject", "operator",
                "--session", "sess-1",
            ]
        )
        rc = run_grant_command(args)
        assert rc == 2
        err = capsys.readouterr().err
        assert "refusing unattended issuance" in err

    def test_issue_json_output(self, capsys):
        args = _make_parser().parse_args(
            [
                "grant", "issue",
                "--operation", "CREATE_FILESYSTEM",
                "--vm", "101",
                "--hostname", "storage-guest",
                "--device", "/dev/sdb1",
                "--fs", "ext4",
                "--label", "DATA",
                "--subject", "operator",
                "--session", "sess-1",
                "--json",
            ]
        )
        rc = run_grant_command(args)
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["authorization_source"] == "USER"
        assert data["device"] == "/dev/sdb1"
        assert data["operation"] == "CREATE_FILESYSTEM"
