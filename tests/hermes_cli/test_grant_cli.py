"""CLI wiring tests for ``hermes grant`` (trusted user boundary)."""

import argparse
import json

import pytest

from hermes_cli.subcommands.grant import build_grant_parser, run_grant_command


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
                "--vm", "148",
                "--hostname", "hp-mail",
                "--device", "/dev/sdb1",
                "--fs", "ext4",
                "--label", "MAILCOW_DOCKER",
                "--subject", "Ed",
                "--session", "sess-1",
                "--ttl", "300",
            ]
        )
        assert args.grant_command == "issue"
        assert args.vm_id == "148"
        assert args.device == "/dev/sdb1"
        assert args.fs_type == "ext4"
        assert args.label == "MAILCOW_DOCKER"
        assert args.subject == "Ed"
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
                "--vm", "148",
                "--hostname", "hp-mail",
                "--device", "/dev/sdb1",
                "--fs", "ext4",
                "--label", "MAILCOW_DOCKER",
                "--subject", "Ed",
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
        assert "MAILCOW_DOCKER" in out

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
                "--vm", "148",
                "--hostname", "hp-mail",
                "--device", "/dev/sda1",
                "--fs", "ext4",
                "--label", "X",
                "--subject", "Ed",
                "--session", "sess-1",
            ]
        )
        rc = run_grant_command(args)
        assert rc == 2
        err = capsys.readouterr().err
        assert "root/whole-disk" in err

    def test_issue_json_output(self, capsys):
        args = _make_parser().parse_args(
            [
                "grant", "issue",
                "--operation", "CREATE_FILESYSTEM",
                "--vm", "148",
                "--hostname", "hp-mail",
                "--device", "/dev/sdb1",
                "--fs", "ext4",
                "--label", "MAILCOW_DOCKER",
                "--subject", "Ed",
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
