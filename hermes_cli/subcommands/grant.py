"""``hermes grant`` — manage explicitly approved one-shot destructive grants.

Issuance requires both:

* a live, read-only capture of the target guest incarnation (hostname,
  ``boot_id`` and ``product_uuid``) through the structured QGA adapter; and
* a correlated, one-shot human approval receipt produced by the existing
  approval layer after an explicit ``approve once`` decision.

The CLI itself is not the authority proof. The receipt and the authenticated
process-bound grant are the security boundary; unattended/bypass contexts are
refused by ``tools.grant_authority``.

Example::

    hermes grant issue --operation CREATE_FILESYSTEM --vm 101 \
        --hostname storage-guest --device /dev/sdb1 --fs ext4 \
        --label DATA --subject operator --session <session-id> [--ttl 600]

    hermes grant list
    hermes grant revoke <grant_id>
    hermes grant audit [--limit 200]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Dict

from tools.destructive_grants import GrantError


def _obtain_human_receipt(args) -> Dict[str, object]:
    """Obtain a correlated, one-shot human approval receipt.

    The approval layer routes through a real human surface (configured
    transport, gateway round-trip, or standard interactive CLI prompt) and
    refuses yolo, mode-off, cron, unattended and single-query bypass contexts.
    The caller never supplies a free-form evidence object.
    """
    from tools.grant_authority import ReceiptError, request_destructive_grant_approval

    try:
        receipt = request_destructive_grant_approval(
            operation=args.operation,
            vm_id=args.vm_id,
            device=args.device,
            fs_type=args.fs_type,
            label=args.label,
            session_id=args.session_id,
            ttl_seconds=args.ttl,
        )
    except ReceiptError as exc:
        raise GrantError(str(exc)) from exc
    return receipt.to_dict()


def build_grant_parser(subparsers, *, cmd_grant=None) -> None:
    parser = subparsers.add_parser(
        "grant",
        help="Issue and manage explicitly approved one-shot destructive grants.",
    )
    grant_sub = parser.add_subparsers(dest="grant_command")

    issue = grant_sub.add_parser(
        "issue",
        help="Issue a one-shot grant for an exact destructive operation.",
    )
    issue.add_argument(
        "--operation",
        required=True,
        help="Operation, e.g. CREATE_FILESYSTEM.",
    )
    issue.add_argument("--vm", dest="vm_id", required=True, help="Target VM id.")
    issue.add_argument("--hostname", required=True, help="Expected guest hostname.")
    issue.add_argument(
        "--device",
        required=True,
        help="Exact validated block-device path, e.g. /dev/sdb1.",
    )
    issue.add_argument(
        "--fs",
        dest="fs_type",
        required=True,
        help="Filesystem type (ext4|xfs).",
    )
    issue.add_argument("--label", required=True, help="Filesystem label (1-16 chars).")
    issue.add_argument(
        "--subject",
        required=True,
        help="Human-readable audit subject (not an authority credential).",
    )
    issue.add_argument(
        "--session",
        dest="session_id",
        required=True,
        help="Hermes session id bound to the approval and grant.",
    )
    issue.add_argument(
        "--ttl",
        type=int,
        default=600,
        help="Requested lifetime in seconds (default 600, max 3600).",
    )
    issue.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    grant_sub.add_parser("list", help="List live grants.")
    grant_sub.add_parser("audit", help="Show the grant audit trail (no secrets).")

    revoke = grant_sub.add_parser("revoke", help="Revoke a live grant.")
    revoke.add_argument("grant_id", help="Opaque grant id.")

    if cmd_grant is not None:
        for sub in (issue, revoke):
            sub.set_defaults(func=cmd_grant)
        for name in ("list", "audit"):
            grant_sub.choices[name].set_defaults(func=cmd_grant)


def run_grant_command(args) -> int:
    from tools.destructive_grants import (
        issue_grant,
        list_grants,
        read_audit_trail,
        revoke_grant,
    )

    cmd = getattr(args, "grant_command", None)

    if cmd == "issue":
        try:
            from tools.qga_structured import QgaError, qga_guest_identity

            try:
                identity = qga_guest_identity(args.vm_id)
            except QgaError as exc:
                print(f"ERROR: cannot capture VM incarnation (QGA): {exc}", file=sys.stderr)
                return 2

            if (
                not identity.get("product_uuid")
                or not identity.get("boot_id")
                or not identity.get("hostname")
            ):
                print(
                    "ERROR: VM incarnation incomplete "
                    "(product_uuid/boot_id/hostname missing)",
                    file=sys.stderr,
                )
                return 2
            if identity.get("hostname") != args.hostname:
                print(
                    f"ERROR: live guest hostname {identity.get('hostname')!r} "
                    f"does not match --hostname {args.hostname!r}",
                    file=sys.stderr,
                )
                return 2

            receipt = _obtain_human_receipt(args)

            grant = issue_grant(
                operation=args.operation,
                vm_id=args.vm_id,
                hostname=args.hostname,
                device=args.device,
                fs_type=args.fs_type,
                label=args.label,
                authorization_subject=args.subject,
                session_id=args.session_id,
                receipt_id=str(receipt["receipt_id"]),
                incarnation_product_uuid=str(identity["product_uuid"]),
                incarnation_boot_id=str(identity["boot_id"]),
                incarnation_hostname=str(identity["hostname"]),
                ttl_seconds=args.ttl,
            )
        except GrantError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

        if getattr(args, "json", False):
            print(json.dumps(grant.to_dict(), sort_keys=True, ensure_ascii=False))
        else:
            print(f"grant_id={grant.grant_id}")
            print(f"operation={grant.operation}")
            print(f"vm_id={grant.vm_id}")
            print(f"hostname={grant.hostname}")
            print(f"device={grant.device}")
            print(f"fs_type={grant.fs_type}")
            print(f"label={grant.label}")
            print(f"authorization_subject={grant.authorization_subject}")
            print(f"authorization_source={grant.authorization_source}")
            print(f"session_id={grant.session_id}")
            print(f"incarnation_product_uuid={grant.incarnation_product_uuid}")
            print(f"incarnation_boot_id={grant.incarnation_boot_id}")
            print(f"expires_at={grant.expires_at}")
            print("NOTE: the grant is one-shot and expires automatically.")
        return 0

    if cmd == "list":
        grants = list_grants()
        if not grants:
            print("No live grants.")
            return 0
        for grant in grants:
            print(
                f"{grant.grant_id}  {grant.operation}  vm={grant.vm_id}  "
                f"{grant.device}  {grant.fs_type}  label={grant.label}  "
                f"subject={grant.authorization_subject}  "
                f"expires_at={grant.expires_at}"
            )
        return 0

    if cmd == "audit":
        entries = read_audit_trail(limit=getattr(args, "limit", 200))
        if not entries:
            print("No audit entries.")
            return 0
        for entry in entries:
            print(json.dumps(entry, sort_keys=True, ensure_ascii=False))
        return 0

    if cmd == "revoke":
        try:
            ok = revoke_grant(args.grant_id)
        except OSError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        if ok:
            print(f"revoked={args.grant_id}")
            return 0
        print(f"not_found={args.grant_id}", file=sys.stderr)
        return 1

    print("usage: hermes grant {issue,list,revoke,audit}", file=sys.stderr)
    return 2
