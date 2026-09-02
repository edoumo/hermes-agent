"""Governed mkfs tool — the only path that can create a filesystem.

This tool is the structured alternative to the hardline-blocked ``mkfs`` in
the generic terminal.  It implements the full mandate workflow:

    grant_id (opaque, issued by the user at a trusted boundary)
    -> claim_grant (atomic reservation BEFORE any effect; one winner)
    -> verify_grant (exact tuple: operation/vm/device/fs/label/session)
    -> qga_guest_identity (durable generation fencing: boot_id + hostname
       must match the incarnation captured at issue time)
    -> qga_prechecks (all mandatory checks, fail-closed)
    -> TOCTOU recheck (identity unchanged since precheck, boot_id included)
    -> qga_create_filesystem (argv built from allowlisted fields only)
    -> qga_postcheck (fs type + label + uuid)
    -> settle_grant (durable settlement: completed / failed_pre_effect /
       indeterminate; the grant NEVER returns to the pool once claimed)

Red lines enforced here:

* The generic terminal policy is untouched: ``mkfs`` stays hardline.
* No shell string is ever built from model input.
* ``--yolo``, approvals.mode=off, allowlist, cron approve and ``force``
  cannot authorize this path: the grant is the ONLY authority, and it is
  verified against the exact tuple plus the live session id.
* The model never sees grant internals — only the opaque ``grant_id``.
* A mutation may have happened as soon as the execution step starts: any
  failure from that point on is settled ``indeterminate`` and reported as
  INDETERMINATE, never as a retryable DENY (review #100694 blocker 2;
  #90144/#90145).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Dict, List, Optional

from tools.destructive_grants import (
    GrantConsumedError,
    GrantDeniedError,
    GrantError,
    GrantInFlightError,
    GrantNotFoundError,
    claim_grant,
    load_grant,
    settle_grant,
    verify_grant,
)
from tools.qga_structured import (
    QgaError,
    qga_create_filesystem,
    qga_guest_identity,
    qga_postcheck,
    qga_prechecks,
)

logger = logging.getLogger(__name__)

GOVERNED_MKFS_SCHEMA = {
    "name": "governed_mkfs",
    "description": (
        "Create a filesystem on an exact, pre-authorized target using a "
        "one-shot capability grant issued by the user (hermes grant issue). "
        "The generic terminal mkfs remains blocked; this is the ONLY governed "
        "path. Requires: grant_id (opaque), vm_id, device (exact partition), "
        "fs_type, label. All prechecks run fail-closed inside the guest; the "
        "grant is consumed atomically after success and can never be replayed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "grant_id": {
                "type": "string",
                "description": "Opaque one-shot grant id issued by the user via `hermes grant issue`.",
            },
            "vm_id": {
                "type": "string",
                "description": "Proxmox VM id, e.g. '148'.",
            },
            "device": {
                "type": "string",
                "description": "Exact block device path, e.g. '/dev/sdb1'. Whole disks and root disks are rejected.",
            },
            "fs_type": {
                "type": "string",
                "enum": ["ext4", "xfs"],
                "description": "Filesystem type (trusted allowlist).",
            },
            "label": {
                "type": "string",
                "description": "Filesystem label, 1-16 chars [A-Za-z0-9_.-].",
            },
        },
        "required": ["grant_id", "vm_id", "device", "fs_type", "label"],
    },
}


def _deny(reason: str, *, decision: str = "DENY", **extra: object) -> str:
    payload: Dict[str, object] = {
        "decision": decision,
        "reason": reason,
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _allow(result: Dict[str, object]) -> str:
    payload: Dict[str, object] = {"decision": "ALLOW"}
    payload.update(result)
    return json.dumps(payload, ensure_ascii=False)


def _prechecks_pass(checks: Dict[str, object]) -> List[str]:
    """Fail-closed evaluation of the mandatory prechecks (mandate §13).

    Any UNKNOWN/AMBIGUOUS/FAIL state -> DENY.  Returns the list of failures
    (empty == PASS).
    """
    failures: List[str] = []

    if not checks.get("device_exists"):
        failures.append("device_exists=NO")
    if not checks.get("is_block_device"):
        failures.append("is_block_device=NO")
    if checks.get("mounted"):
        failures.append("mounted=YES")
    if checks.get("swap"):
        failures.append("swap=YES")
    if checks.get("filesystem_existing"):
        failures.append("filesystem_existing=YES")
    if checks.get("filesystem_signature"):
        failures.append("filesystem_signature=YES")
    if checks.get("lvm_member"):
        failures.append("lvm_member=YES")
    if checks.get("mdraid_member"):
        failures.append("mdraid_member=YES")
    holders = checks.get("holders")
    if isinstance(holders, list) and holders:
        failures.append(f"holders={holders}")
    if checks.get("docker_use"):
        failures.append("docker_use=YES")
    if checks.get("fstab_use"):
        failures.append("fstab_use=YES")
    if "lsblk_parse_error" in checks:
        failures.append("lsblk_parse_error=YES")

    return failures


def _identity_snapshot(checks: Dict[str, object]) -> Dict[str, object]:
    """The identity fields that must be unchanged between precheck and action."""
    return {
        "device": checks.get("device"),
        "major_minor": checks.get("major_minor"),
        "size_bytes": checks.get("size_bytes"),
        "mounted": checks.get("mounted"),
        "filesystem_existing": checks.get("filesystem_existing"),
        "filesystem_signature": checks.get("filesystem_signature"),
        "holders": checks.get("holders"),
        "boot_id": checks.get("boot_id"),
    }


def _handle_governed_mkfs(args: Dict[str, object], **kwargs) -> str:
    grant_id = args.get("grant_id")
    vm_id = args.get("vm_id")
    device = args.get("device")
    fs_type = args.get("fs_type")
    label = args.get("label")
    session_id = kwargs.get("session_id") or ""

    if not isinstance(grant_id, str) or not grant_id:
        return _deny("grant_id_required")
    if not isinstance(vm_id, str) or not vm_id:
        return _deny("vm_id_required")
    if not isinstance(device, str) or not device:
        return _deny("device_required")
    if not isinstance(fs_type, str) or not fs_type:
        return _deny("fs_type_required")
    if not isinstance(label, str) or not label:
        return _deny("label_required")
    if not session_id:
        return _deny("session_id_required", reason_detail="governed_mkfs requires a live session context")

    # 0. Atomic claim BEFORE any effect (review #100694 blocker 2).  The claim
    #    is a rename-based reservation: exactly one concurrent caller wins;
    #    the loser is denied before any QGA call.  The claim is bound to this
    #    execution id, which the settlement later re-checks.
    execution_id = f"{session_id}:{grant_id}"
    try:
        claim_grant(grant_id, execution_id)
    except GrantInFlightError as exc:
        return _deny("claim_lost", detail=str(exc))
    except GrantConsumedError as exc:
        return _deny("grant_denied", detail=str(exc))
    except GrantNotFoundError as exc:
        return _deny("grant_denied", detail=str(exc))
    except GrantDeniedError as exc:
        return _deny("grant_denied", detail=str(exc))
    except GrantError as exc:
        return _deny("grant_error", detail=str(exc))

    # 1. Grant verification (exact tuple + expiry + integrity).  Any mismatch
    #    raises and is recorded in the audit trail.  A claimed grant is
    #    rejected here (no double-claim).
    try:
        grant = verify_grant(
            grant_id,
            operation="CREATE_FILESYSTEM",
            vm_id=vm_id,
            device=device,
            fs_type=fs_type,
            label=label,
            session_id=session_id,
            claimed_by_execution=execution_id,
        )
    except GrantDeniedError as exc:
        settle_grant(grant_id, execution_id, "failed_pre_effect")
        return _deny("grant_denied", detail=str(exc), outcome="failed_pre_effect")
    except GrantNotFoundError as exc:
        settle_grant(grant_id, execution_id, "failed_pre_effect")
        return _deny("grant_denied", detail=str(exc), outcome="failed_pre_effect")
    except GrantError as exc:
        settle_grant(grant_id, execution_id, "failed_pre_effect")
        return _deny("grant_error", detail=str(exc), outcome="failed_pre_effect")

    # 2. Durable generation fencing at the sink (review #100694 blocker 3;
    #    #90145).  The incarnation captured at issue time must match the live
    #    guest right now.  A replaced/rebooted VM (ABA) is denied BEFORE any
    #    mutation.
    try:
        identity = qga_guest_identity(vm_id)
    except QgaError as exc:
        settle_grant(grant_id, execution_id, "failed_pre_effect")
        return _deny(
            "incarnation_unavailable", detail=str(exc), outcome="failed_pre_effect",
        )

    if (
        identity.get("product_uuid") != grant.incarnation_product_uuid
        or identity.get("boot_id") != grant.incarnation_boot_id
        or identity.get("hostname") != grant.incarnation_hostname
    ):
        settle_grant(grant_id, execution_id, "failed_pre_effect")
        return _deny(
            "incarnation_changed",
            expected_product_uuid=grant.incarnation_product_uuid,
            observed_product_uuid=identity.get("product_uuid"),
            expected_boot_id=grant.incarnation_boot_id,
            observed_boot_id=identity.get("boot_id"),
            expected_hostname=grant.incarnation_hostname,
            observed_hostname=identity.get("hostname"),
            outcome="failed_pre_effect",
        )

    # 3. Mandatory prechecks inside the guest (fail-closed).
    try:
        prechecks = qga_prechecks(vm_id, device)
    except QgaError as exc:
        settle_grant(grant_id, execution_id, "failed_pre_effect")
        return _deny(
            "prechecks_unavailable", detail=str(exc), outcome="failed_pre_effect",
        )

    failures = _prechecks_pass(prechecks)
    if failures:
        settle_grant(grant_id, execution_id, "failed_pre_effect")
        return _deny(
            "prechecks_failed", failures=failures, outcome="failed_pre_effect",
        )

    # 4. TOCTOU recheck: identity must be unchanged since the precheck.
    #    The precheck and the action run back-to-back over the same QGA
    #    channel; we re-read the critical identity fields immediately before
    #    executing and compare (boot_id included: a reboot between precheck
    #    and action is a generation change).
    try:
        recheck = qga_prechecks(vm_id, device)
    except QgaError as exc:
        settle_grant(grant_id, execution_id, "failed_pre_effect")
        return _deny(
            "toctou_recheck_unavailable", detail=str(exc),
            outcome="failed_pre_effect",
        )

    recheck_failures = _prechecks_pass(recheck)
    if recheck_failures:
        settle_grant(grant_id, execution_id, "failed_pre_effect")
        return _deny(
            "toctou_recheck_failed", failures=recheck_failures,
            outcome="failed_pre_effect",
        )

    before = _identity_snapshot(prechecks)
    after = _identity_snapshot(recheck)
    if before != after:
        settle_grant(grant_id, execution_id, "failed_pre_effect")
        return _deny(
            "toctou_identity_changed", before=before, after=after,
            outcome="failed_pre_effect",
        )

    # 5. Structured execution (argv built from allowlisted fields only).
    #    From this point on a mutation MAY have happened: any failure is
    #    settled as INDETERMINATE and the grant never returns to the pool.
    try:
        exec_result = qga_create_filesystem(vm_id, device, fs_type, label)
    except QgaError as exc:
        settle_grant(grant_id, execution_id, "indeterminate")
        return _deny(
            "execution_failed", detail=str(exc), outcome="indeterminate",
            decision="INDETERMINATE",
        )

    exec_exit = exec_result.get("exit_code")
    if exec_exit != 0:
        settle_grant(grant_id, execution_id, "indeterminate")
        return _deny(
            "execution_exit_nonzero",
            exit_code=exec_exit,
            err_data=str(exec_result.get("err_data", ""))[:500],
            outcome="indeterminate",
            decision="INDETERMINATE",
        )

    # 6. Postcheck: filesystem type + label + uuid.
    try:
        postcheck = qga_postcheck(vm_id, device, fs_type, label)
    except QgaError as exc:
        settle_grant(grant_id, execution_id, "indeterminate")
        return _deny(
            "postcheck_unavailable", detail=str(exc), outcome="indeterminate",
            decision="INDETERMINATE",
        )

    if postcheck.get("filesystem") != fs_type:
        settle_grant(grant_id, execution_id, "indeterminate")
        return _deny(
            "postcheck_fs_mismatch",
            expected=fs_type,
            observed=postcheck.get("filesystem"),
            outcome="indeterminate",
            decision="INDETERMINATE",
        )
    if postcheck.get("label") != label:
        settle_grant(grant_id, execution_id, "indeterminate")
        return _deny(
            "postcheck_label_mismatch",
            expected=label,
            observed=postcheck.get("label"),
            outcome="indeterminate",
            decision="INDETERMINATE",
        )

    # 7. Durable settlement: the grant is permanently out of the pool.  A
    #    second use of the same grant is denied (replay protection).
    try:
        settle_grant(grant_id, execution_id, "completed")
    except GrantError as exc:
        return _deny(
            "settle_failed", detail=str(exc), outcome="indeterminate",
            decision="INDETERMINATE",
        )

    return _allow(
        {
            "operation": "CREATE_FILESYSTEM",
            "vm_id": vm_id,
            "device": device,
            "fs_type": fs_type,
            "label": label,
            "uuid": postcheck.get("uuid"),
            "guest_argv": exec_result.get("guest_argv"),
            "exit_code": exec_result.get("exit_code"),
            "capability_consumed": True,
            "grant_id": grant_id,
            "outcome": "completed",
        }
    )


def check_governed_mkfs_requirements() -> bool:
    """Availability: the governed path needs the QGA SSH key and node."""
    from tools.qga_structured import DEFAULT_QGA_SSH_KEY

    import os

    return os.path.exists(DEFAULT_QGA_SSH_KEY)


def register_governed_mkfs() -> None:
    from tools.registry import registry

    registry.register(
        name="governed_mkfs",
        toolset="terminal",
        schema=GOVERNED_MKFS_SCHEMA,
        handler=_handle_governed_mkfs,
        check_fn=check_governed_mkfs_requirements,
        emoji="🛡️",
        max_result_size_chars=20_000,
    )


# Direct module-level registration: the registry's AST-based discovery only
# picks up top-level ``registry.register(...)`` calls (see
# tools/registry.py::_module_registers_tools).  A call wrapped in a function
# would silently leave the tool undiscovered.
from tools.registry import registry  # noqa: E402

registry.register(
    name="governed_mkfs",
    toolset="terminal",
    schema=GOVERNED_MKFS_SCHEMA,
    handler=_handle_governed_mkfs,
    check_fn=check_governed_mkfs_requirements,
    emoji="🛡️",
    max_result_size_chars=20_000,
)
