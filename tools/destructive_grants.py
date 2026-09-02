"""Trusted one-shot destructive grants.

A ``DestructiveGrant`` is the only representation of an explicit user GO that
the governed destructive path accepts.  Grants are created exclusively at a
trusted user boundary (``hermes grant issue`` run by the user, or a gateway
hook that has authenticated the user's own message) — never by the model.

Design invariants (see docs/destructive-actions/HERMES_DESTRUCTIVE_ACTION_POLICY.md):

1. The grant is bound to ``operation/host-or-vm/device/filesystem/label``.
2. It is one-shot: consumption is atomic (rename-based) and replay is denied.
3. It is short-lived: ``expires_at`` is enforced on every use.
4. The model only ever sees the opaque ``grant_id``; all fields live in a
   host-side store (``~/.hermes/grants/``, mode 0600) that generic tools do
   not read.
5. ``authorization_subject`` records who authorized (the user), and
   ``authorization_source`` is always ``USER`` — an agent-generated grant is
   structurally impossible because the issue path is not reachable from any
   model tool.
6. Every decision (issue / allow / deny / consume) is appended to the audit
   trail without secrets.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GRANT_DIR_NAME = "grants"
AUDIT_FILE_NAME = "audit.jsonl"
DEFAULT_TTL_SECONDS = 600  # 10 minutes, per mandate §10 (short lifetime)
MAX_TTL_SECONDS = 3600

# Operations the governed path understands.  Anything else is rejected at
# issue time, so a grant can never be minted for an unknown operation.
VALID_OPERATIONS = ("CREATE_FILESYSTEM",)

# Filesystem types with a trusted binary mapping (mandate §15: the model must
# never produce ``binary=<user controlled string>``).
FS_TYPE_TO_BINARY = {
    "ext4": "/usr/sbin/mkfs.ext4",
    "xfs": "/usr/sbin/mkfs.xfs",
}

# Strict device allowlist.  A partition-less whole disk (``/dev/sda``) is
# rejected: the governed path only ever formats an exact partition or an
# explicit disposable loop device, never a whole disk and never a root disk.
_DEVICE_RE = re.compile(
    r"^/dev/(?:sd[a-z][0-9]+|vd[a-z][0-9]+|nvme[0-9]+n[0-9]+p[0-9]+|"
    r"mmcblk[0-9]+p[0-9]+|loop[0-9]+)$"
)
# Root/whole-disk prefixes that must never be formatted.  These are the
# conventional system disks (sda, vda, nvme0n1, mmcblk0) and ALL their
# partitions: a partition of the system disk is the root/root-parent case
# the mandate forbids.  Secondary data disks (sdb, sdc, ...) remain issuable
# as exact partitions.
_FORBIDDEN_DEVICE_PREFIXES = (
    "/dev/sda", "/dev/vda", "/dev/nvme0n1", "/dev/mmcblk0",
)

# Strict label validation: ext4 labels are <= 16 chars, no spaces, no slashes.
_LABEL_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,16}$")

# VM id: positive integer.
_VM_ID_RE = re.compile(r"^[0-9]{1,5}$")


class GrantError(Exception):
    """Base error for grant operations."""


class GrantNotFoundError(GrantError):
    pass


class GrantDeniedError(GrantError):
    """Raised when a grant exists but the request does not match it."""


class GrantExpiredError(GrantDeniedError):
    pass


class GrantConsumedError(GrantDeniedError):
    pass


class GrantInFlightError(GrantDeniedError):
    """Raised when a grant is claimed by another execution (concurrent use)."""


# Outcomes a grant can be settled with.  ``completed`` is the only success;
# ``failed_pre_effect`` means no mutation can have happened; ``indeterminate``
# means a mutation may have happened and the grant must NEVER return to the
# pool (no blind retry, per #90144/#90145 and the review of #100694).
VALID_SETTLEMENT_OUTCOMES = ("completed", "failed_pre_effect", "indeterminate")


@dataclass(frozen=True)
class DestructiveGrant:
    """Immutable one-shot capability record."""

    grant_id: str
    operation: str
    vm_id: str
    hostname: str
    device: str
    fs_type: str
    label: str
    authorization_subject: str
    authorization_source: str  # always "USER"
    session_id: str
    issued_at: float
    expires_at: float
    nonce: str
    binding_sha256: str
    authorization_evidence: Dict[str, object]
    incarnation_product_uuid: str
    incarnation_boot_id: str
    incarnation_hostname: str
    consumed: bool = False
    consumed_at: Optional[float] = None
    claimed_by_execution: Optional[str] = None
    claimed_at: Optional[float] = None
    settlement_outcome: Optional[str] = None
    settled_at: Optional[float] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "grant_id": self.grant_id,
            "operation": self.operation,
            "vm_id": self.vm_id,
            "hostname": self.hostname,
            "device": self.device,
            "fs_type": self.fs_type,
            "label": self.label,
            "authorization_subject": self.authorization_subject,
            "authorization_source": self.authorization_source,
            "session_id": self.session_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "binding_sha256": self.binding_sha256,
            "authorization_evidence": self.authorization_evidence,
            "incarnation_product_uuid": self.incarnation_product_uuid,
            "incarnation_boot_id": self.incarnation_boot_id,
            "incarnation_hostname": self.incarnation_hostname,
            "consumed": self.consumed,
            "consumed_at": self.consumed_at,
            "claimed_by_execution": self.claimed_by_execution,
            "claimed_at": self.claimed_at,
            "settlement_outcome": self.settlement_outcome,
            "settled_at": self.settled_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "DestructiveGrant":
        consumed_at_raw = data.get("consumed_at")
        claimed_at_raw = data.get("claimed_at")
        settled_at_raw = data.get("settled_at")
        return cls(
            grant_id=str(data["grant_id"]),
            operation=str(data["operation"]),
            vm_id=str(data["vm_id"]),
            hostname=str(data["hostname"]),
            device=str(data["device"]),
            fs_type=str(data["fs_type"]),
            label=str(data["label"]),
            authorization_subject=str(data["authorization_subject"]),
            authorization_source=str(data["authorization_source"]),
            session_id=str(data["session_id"]),
            issued_at=float(str(data["issued_at"])),
            expires_at=float(str(data["expires_at"])),
            nonce=str(data["nonce"]),
            binding_sha256=str(data["binding_sha256"]),
            authorization_evidence=dict(data.get("authorization_evidence") or {}),
            incarnation_product_uuid=str(data.get("incarnation_product_uuid") or ""),
            incarnation_boot_id=str(data.get("incarnation_boot_id") or ""),
            incarnation_hostname=str(data.get("incarnation_hostname") or ""),
            consumed=bool(data.get("consumed", False)),
            consumed_at=(
                float(str(consumed_at_raw)) if consumed_at_raw is not None else None
            ),
            claimed_by_execution=(
                str(data["claimed_by_execution"])
                if data.get("claimed_by_execution") is not None
                else None
            ),
            claimed_at=(
                float(str(claimed_at_raw)) if claimed_at_raw is not None else None
            ),
            settlement_outcome=(
                str(data["settlement_outcome"])
                if data.get("settlement_outcome") is not None
                else None
            ),
            settled_at=(
                float(str(settled_at_raw)) if settled_at_raw is not None else None
            ),
        )


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _grants_dir() -> Path:
    from hermes_constants import get_hermes_home

    d = Path(get_hermes_home()) / GRANT_DIR_NAME
    return d


def _grant_path(grant_id: str) -> Path:
    # grant_id is an opaque UUID we mint; still guard against path traversal.
    if not re.match(r"^[0-9a-f\-]{36}$", grant_id):
        raise GrantError(f"invalid grant id format: {grant_id!r}")
    return _grants_dir() / f"{grant_id}.json"


def _audit_path() -> Path:
    return _grants_dir() / AUDIT_FILE_NAME


def _ensure_dir() -> None:
    d = _grants_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_device(device: str) -> None:
    """Strict device validation.  Raises GrantError on any non-conforming value."""
    if not isinstance(device, str) or not device:
        raise GrantError("device is required")
    if not _DEVICE_RE.match(device):
        raise GrantError(f"device {device!r} is not an allowed exact partition/loop path")
    for prefix in _FORBIDDEN_DEVICE_PREFIXES:
        if device.startswith(prefix):
            # Covers the whole disk itself (/dev/sda) and every partition of
            # a system disk (/dev/sda1, /dev/nvme0n1p1, /dev/mmcblk0p1).
            raise GrantError(f"device {device!r} is a root/whole-disk path and is forbidden")
    # Explicitly forbid the exact whole-disk names (no partition number).
    if re.match(r"^/dev/(?:sd|vd)[a-z]+$", device):
        raise GrantError(f"device {device!r} is a whole disk; an exact partition is required")
    if re.match(r"^/dev/nvme[0-9]+n[0-9]+$", device):
        raise GrantError(f"device {device!r} is a whole disk; an exact partition is required")


def validate_fs_type(fs_type: str) -> None:
    if fs_type not in FS_TYPE_TO_BINARY:
        raise GrantError(f"filesystem {fs_type!r} is not in the trusted allowlist")


def validate_label(label: str) -> None:
    if not isinstance(label, str) or not _LABEL_RE.match(label):
        raise GrantError(f"label {label!r} is invalid (1-16 chars, [A-Za-z0-9_.-])")


def validate_vm_id(vm_id: str) -> None:
    if not isinstance(vm_id, str) or not _VM_ID_RE.match(vm_id):
        raise GrantError(f"vm_id {vm_id!r} is invalid")


def validate_operation(operation: str) -> None:
    if operation not in VALID_OPERATIONS:
        raise GrantError(f"operation {operation!r} is not supported")


def _binding_sha256(
    grant_id: str,
    operation: str,
    vm_id: str,
    hostname: str,
    device: str,
    fs_type: str,
    label: str,
    subject: str,
    session_id: str,
    nonce: str,
    issued_at: float,
    expires_at: float,
    evidence: Dict[str, object],
    incarnation_product_uuid: str,
    incarnation_boot_id: str,
    incarnation_hostname: str,
) -> str:
    """Canonical binding over the FULL grant identity.

    Covers the grant id itself (a clone under a second UUID breaks the
    hash even when the embedded id is rewritten), the operation tuple, the
    human authorization evidence, the VM incarnation (durable generation:
    product_uuid + boot_id + hostname), and the exact validity window
    (``issued_at``/``expires_at``).  Any tamper — clone, TTL extension,
    evidence swap, incarnation swap — breaks the recomputed hash and is
    treated as DENY (review #100694 blocker 1; #90144/#90145).
    """
    canonical = "|".join(
        [
            grant_id,
            operation,
            vm_id,
            hostname,
            device,
            fs_type,
            label,
            subject,
            session_id,
            nonce,
            repr(issued_at),
            repr(expires_at),
            json.dumps(evidence, sort_keys=True, separators=(",", ":")),
            incarnation_product_uuid,
            incarnation_boot_id,
            incarnation_hostname,
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


def _audit(entry: Dict[str, object]) -> None:
    try:
        _ensure_dir()
        line = json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n"
        with open(_audit_path(), "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:  # pragma: no cover - audit must never break the action
        logger.error("grant audit write failed: %s", exc)


def read_audit_trail(limit: int = 200) -> List[Dict[str, object]]:
    """Read the audit trail (newest first).  Used by ``hermes grant audit``."""
    p = _audit_path()
    if not p.exists():
        return []
    entries: List[Dict[str, object]] = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return list(reversed(entries[-limit:]))


# ---------------------------------------------------------------------------
# Issue (trusted user boundary only)
# ---------------------------------------------------------------------------


def issue_grant(
    *,
    operation: str,
    vm_id: str,
    hostname: str,
    device: str,
    fs_type: str,
    label: str,
    authorization_subject: str,
    session_id: str,
    authorization_evidence: Dict[str, object],
    incarnation_product_uuid: str,
    incarnation_boot_id: str,
    incarnation_hostname: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> DestructiveGrant:
    """Create a one-shot grant.  ONLY callable from a trusted user boundary.

    This function is deliberately not reachable from any model tool: it lives
    in a module that no tool handler imports, and the CLI subcommand that
    wraps it runs in the user's own shell.

    ``authorization_evidence`` is the correlated human approval decision
    (request_id + request_digest + decision + principal + surface) produced
    by the host approval transport; ``incarnation_product_uuid`` /
    ``incarnation_boot_id`` / ``incarnation_hostname`` are the durable VM
    generation captured at issue time (review #100694 blocker 1 + blocker 3;
    #90144/#90145).
    """
    validate_operation(operation)
    validate_vm_id(vm_id)
    validate_device(device)
    validate_fs_type(fs_type)
    validate_label(label)

    if not isinstance(authorization_subject, str) or not authorization_subject:
        raise GrantError("authorization_subject is required (the human who grants)")
    if not isinstance(session_id, str) or not session_id:
        raise GrantError("session_id is required")
    if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise GrantError("ttl_seconds must be a positive integer")
    ttl_seconds = min(ttl_seconds, MAX_TTL_SECONDS)

    # B1: a grant is only minted from a correlated, one-shot human decision.
    if not isinstance(authorization_evidence, dict) or not authorization_evidence:
        raise GrantError("authorization_evidence is required (correlated human decision)")
    for key in ("request_id", "request_digest", "decision", "principal", "surface"):
        if not authorization_evidence.get(key):
            raise GrantError(f"authorization_evidence.{key} is required")
    if authorization_evidence.get("decision") != "once":
        raise GrantError(
            "authorization_evidence.decision must be 'once' "
            "(a one-shot grant cannot be minted from a session/permanent approval)"
        )

    # B3: the durable VM incarnation must be captured at issue time.
    if not isinstance(incarnation_product_uuid, str) or not incarnation_product_uuid:
        raise GrantError("incarnation_product_uuid is required (stable guest identity)")
    if not isinstance(incarnation_boot_id, str) or not incarnation_boot_id:
        raise GrantError("incarnation_boot_id is required (durable VM generation)")
    if not isinstance(incarnation_hostname, str) or not incarnation_hostname:
        raise GrantError("incarnation_hostname is required")

    _ensure_dir()
    now = time.time()
    nonce = secrets.token_hex(16)
    grant_id = str(uuid.uuid4())
    binding = _binding_sha256(
        grant_id, operation, vm_id, hostname, device, fs_type, label,
        authorization_subject, session_id, nonce,
        now, now + ttl_seconds,
        authorization_evidence, incarnation_product_uuid,
        incarnation_boot_id, incarnation_hostname,
    )
    grant = DestructiveGrant(
        grant_id=grant_id,
        operation=operation,
        vm_id=vm_id,
        hostname=hostname,
        device=device,
        fs_type=fs_type,
        label=label,
        authorization_subject=authorization_subject,
        authorization_source="USER",
        session_id=session_id,
        issued_at=now,
        expires_at=now + ttl_seconds,
        nonce=nonce,
        binding_sha256=binding,
        authorization_evidence=dict(authorization_evidence),
        incarnation_product_uuid=incarnation_product_uuid,
        incarnation_boot_id=incarnation_boot_id,
        incarnation_hostname=incarnation_hostname,
    )

    path = _grant_path(grant_id)
    # O_EXCL: a grant id collision must never overwrite an existing grant.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(grant.to_dict(), fh, sort_keys=True, ensure_ascii=False)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    _audit(
        {
            "event": "grant_issued",
            "grant_id": grant_id,
            "operation": operation,
            "vm_id": vm_id,
            "hostname": hostname,
            "device": device,
            "fs_type": fs_type,
            "label": label,
            "authorization_subject": authorization_subject,
            "authorization_source": "USER",
            "session_id": session_id,
            "ttl_seconds": ttl_seconds,
            "expires_at": grant.expires_at,
            "binding_sha256": binding,
            "incarnation_product_uuid": incarnation_product_uuid,
            "incarnation_boot_id": incarnation_boot_id,
            "evidence_request_id": authorization_evidence.get("request_id"),
            "evidence_decision": authorization_evidence.get("decision"),
        }
    )
    logger.info("destructive grant %s issued for %s %s", grant_id, operation, device)
    return grant


# ---------------------------------------------------------------------------
# Load / verify / consume
# ---------------------------------------------------------------------------


def load_grant(grant_id: str) -> DestructiveGrant:
    """Load a grant by opaque id.

    Raises GrantNotFoundError if absent, GrantConsumedError if the grant was
    already consumed (replay), GrantInFlightError if the grant is claimed by
    another execution, and GrantDeniedError if the file was tampered.
    """
    path = _grant_path(grant_id)
    if not path.exists():
        # A claimed grant lives under the .claimed suffix (in-flight for the
        # claiming execution); a consumed/settled grant is replay.
        claimed_path = path.with_suffix(".json.claimed")
        if claimed_path.exists():
            path = claimed_path
        else:
            consumed_path = path.with_suffix(".json.consumed")
            if consumed_path.exists():
                raise GrantConsumedError(f"grant {grant_id!r} already consumed (replay denied)")
            settled_path = path.with_suffix(".json.settled")
            if settled_path.exists():
                raise GrantConsumedError(f"grant {grant_id!r} already settled (replay denied)")
            raise GrantNotFoundError(f"grant {grant_id!r} not found")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise GrantError(f"grant {grant_id!r} unreadable: {exc}") from exc
    grant = DestructiveGrant.from_dict(data)
    # B1: the payload must be bound to the path id.  A clone under a second
    # UUID (with or without rewriting the embedded id) is structurally denied.
    if grant.grant_id != grant_id:
        raise GrantDeniedError(
            f"grant {grant_id!r} payload id mismatch (clone denied)"
        )
    # Integrity: the stored binding must match the stored fields.  A tampered
    # file (any field edited, TTL extended, evidence or incarnation swapped)
    # fails here and is treated as DENY.
    recomputed = _binding_sha256(
        grant.grant_id, grant.operation, grant.vm_id, grant.hostname,
        grant.device, grant.fs_type, grant.label, grant.authorization_subject,
        grant.session_id, grant.nonce,
        grant.issued_at, grant.expires_at,
        grant.authorization_evidence, grant.incarnation_product_uuid,
        grant.incarnation_boot_id, grant.incarnation_hostname,
    )
    if recomputed != grant.binding_sha256:
        raise GrantDeniedError(f"grant {grant_id!r} integrity check failed (tampered)")
    return grant


def verify_grant(
    grant_id: str,
    *,
    operation: str,
    vm_id: str,
    device: str,
    fs_type: str,
    label: str,
    session_id: str,
    claimed_by_execution: Optional[str] = None,
) -> DestructiveGrant:
    """Verify a grant against the exact requested tuple.

    Every mismatch (operation, vm, device, fs, label, session) is a DENY.
    Expired and consumed grants are DENY.  A grant claimed by ANOTHER
    execution is DENY (concurrent use); a grant claimed by the calling
    execution (``claimed_by_execution`` matches) verifies normally.
    Returns the grant on success.
    """
    grant = load_grant(grant_id)

    if grant.consumed:
        _audit(
            {
                "event": "grant_denied",
                "grant_id": grant_id,
                "reason": "already_consumed",
                "requested": {
                    "operation": operation, "vm_id": vm_id, "device": device,
                    "fs_type": fs_type, "label": label, "session_id": session_id,
                },
            }
        )
        raise GrantConsumedError(f"grant {grant_id!r} already consumed (replay denied)")

    # B2: a grant claimed by another execution is in flight — verification
    # must not pass it (no double-claim, no concurrent use).  The claiming
    # execution itself verifies normally.
    if grant.claimed_by_execution is not None:
        if claimed_by_execution != grant.claimed_by_execution:
            _audit(
                {
                    "event": "grant_denied",
                    "grant_id": grant_id,
                    "reason": "already_claimed",
                    "claimed_by_execution": grant.claimed_by_execution,
                    "requested": {
                        "operation": operation, "vm_id": vm_id, "device": device,
                        "fs_type": fs_type, "label": label, "session_id": session_id,
                    },
                }
            )
            raise GrantInFlightError(
                f"grant {grant_id!r} is claimed by execution "
                f"{grant.claimed_by_execution!r} (concurrent use denied)"
            )

    if time.time() > grant.expires_at:
        _audit(
            {
                "event": "grant_denied",
                "grant_id": grant_id,
                "reason": "expired",
                "requested": {
                    "operation": operation, "vm_id": vm_id, "device": device,
                    "fs_type": fs_type, "label": label, "session_id": session_id,
                },
            }
        )
        raise GrantExpiredError(f"grant {grant_id!r} expired")

    mismatches = []
    if operation != grant.operation:
        mismatches.append(f"operation {operation!r} != {grant.operation!r}")
    if vm_id != grant.vm_id:
        mismatches.append(f"vm_id {vm_id!r} != {grant.vm_id!r}")
    if device != grant.device:
        mismatches.append(f"device {device!r} != {grant.device!r}")
    if fs_type != grant.fs_type:
        mismatches.append(f"fs_type {fs_type!r} != {grant.fs_type!r}")
    if label != grant.label:
        mismatches.append(f"label {label!r} != {grant.label!r}")
    if session_id != grant.session_id:
        mismatches.append(f"session_id {session_id!r} != {grant.session_id!r}")

    if mismatches:
        _audit(
            {
                "event": "grant_denied",
                "grant_id": grant_id,
                "reason": "tuple_mismatch",
                "details": mismatches,
                "requested": {
                    "operation": operation, "vm_id": vm_id, "device": device,
                    "fs_type": fs_type, "label": label, "session_id": session_id,
                },
            }
        )
        raise GrantDeniedError("grant tuple mismatch: " + "; ".join(mismatches))

    _audit(
        {
            "event": "grant_verified",
            "grant_id": grant_id,
            "operation": operation,
            "vm_id": vm_id,
            "device": device,
            "fs_type": fs_type,
            "label": label,
            "session_id": session_id,
        }
    )
    return grant


def claim_grant(grant_id: str, execution_id: str) -> DestructiveGrant:
    """Atomically claim a grant for one execution (B2).

    The claim is a rename of the live grant file to a ``.claimed`` suffix,
    which is atomic on POSIX: exactly one concurrent caller wins.  The
    claimed file records ``claimed_by_execution`` and ``claimed_at``.

    Raises GrantNotFoundError if absent, GrantConsumedError if already
    consumed, GrantInFlightError if already claimed by another execution,
    and GrantDeniedError if the file was tampered.
    """
    if not isinstance(execution_id, str) or not execution_id:
        raise GrantError("execution_id is required to claim a grant")

    live = _grant_path(grant_id)
    claimed_path = live.with_suffix(".json.claimed")
    settled_path = live.with_suffix(".json.settled")
    if not live.exists():
        if claimed_path.exists():
            raise GrantInFlightError(
                f"grant {grant_id!r} is already claimed (concurrent use denied)"
            )
        consumed_path = live.with_suffix(".json.consumed")
        if consumed_path.exists():
            raise GrantConsumedError(f"grant {grant_id!r} already consumed (replay denied)")
        if settled_path.exists():
            raise GrantConsumedError(
                f"grant {grant_id!r} already settled (replay denied)"
            )
        raise GrantNotFoundError(f"grant {grant_id!r} not found")

    # Integrity BEFORE the rename: a tampered grant must be reported as
    # tampered, not as "not found" after the claim moved the file away.
    try:
        with open(live, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        # The live file vanished between our existence check and our read:
        # another caller claimed it in that window.  That is a lost claim,
        # not a corrupt grant — report it as in-flight (or as replay if the
        # winner already settled the grant before we reached the read).
        if claimed_path.exists():
            raise GrantInFlightError(
                f"grant {grant_id!r} is already claimed (concurrent use denied)"
            ) from exc
        if settled_path.exists():
            raise GrantConsumedError(
                f"grant {grant_id!r} already settled (replay denied)"
            ) from exc
        raise GrantError(f"grant {grant_id!r} unreadable: {exc}") from exc
    except json.JSONDecodeError as exc:
        # The file may have been truncated by the winning caller between our
        # open() and our read (the winner renames live -> .claimed and then
        # rewrites the claimed file in place, truncating the same inode our
        # fd still points to).  That is a lost claim, not corruption.
        if claimed_path.exists():
            raise GrantInFlightError(
                f"grant {grant_id!r} is already claimed (concurrent use denied)"
            ) from exc
        if settled_path.exists():
            raise GrantConsumedError(
                f"grant {grant_id!r} already settled (replay denied)"
            ) from exc
        raise GrantError(f"grant {grant_id!r} unreadable: {exc}") from exc
    pre_grant = DestructiveGrant.from_dict(data)
    if pre_grant.grant_id != grant_id:
        raise GrantDeniedError(
            f"grant {grant_id!r} payload id mismatch (clone denied)"
        )
    recomputed = _binding_sha256(
        pre_grant.grant_id, pre_grant.operation, pre_grant.vm_id,
        pre_grant.hostname, pre_grant.device, pre_grant.fs_type,
        pre_grant.label, pre_grant.authorization_subject,
        pre_grant.session_id, pre_grant.nonce,
        pre_grant.issued_at, pre_grant.expires_at,
        pre_grant.authorization_evidence, pre_grant.incarnation_product_uuid,
        pre_grant.incarnation_boot_id, pre_grant.incarnation_hostname,
    )
    if recomputed != pre_grant.binding_sha256:
        raise GrantDeniedError(f"grant {grant_id!r} integrity check failed (tampered)")

    claimed_path = live.with_suffix(".json.claimed")
    try:
        os.rename(live, claimed_path)
    except OSError as exc:
        # The rename lost a race: another caller claimed the grant between
        # our integrity read and our rename.  That is a lost claim, not an
        # error — report it as in-flight (or as replay if the winner already
        # settled the grant before we reached the rename).
        if claimed_path.exists():
            raise GrantInFlightError(
                f"grant {grant_id!r} is already claimed (concurrent use denied)"
            ) from exc
        if settled_path.exists():
            raise GrantConsumedError(
                f"grant {grant_id!r} already settled (replay denied)"
            ) from exc
        raise GrantError(f"grant {grant_id!r} claim failed: {exc}") from exc

    try:
        with open(claimed_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise GrantError(f"claimed grant {grant_id!r} unreadable: {exc}") from exc

    data["claimed_by_execution"] = execution_id
    data["claimed_at"] = time.time()
    with open(claimed_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True, ensure_ascii=False)

    grant = DestructiveGrant.from_dict(data)
    _audit(
        {
            "event": "grant_claimed",
            "grant_id": grant_id,
            "execution_id": execution_id,
            "operation": grant.operation,
            "vm_id": grant.vm_id,
            "device": grant.device,
            "fs_type": grant.fs_type,
            "label": grant.label,
        }
    )
    return grant


def settle_grant(grant_id: str, execution_id: str, outcome: str) -> DestructiveGrant:
    """Durably settle a claimed grant (B2).

    ``outcome`` is one of ``completed`` / ``failed_pre_effect`` /
    ``indeterminate``.  The settlement is a rename of the ``.claimed`` file
    to a ``.settled`` suffix (atomic on POSIX) and is bound to the claiming
    execution: a mismatched ``execution_id`` is rejected.

    Once settled, the grant is permanently out of the pool — an
    ``indeterminate`` outcome NEVER returns the grant to the live set (no
    blind retry after a possible mutation, per #90144/#90145).
    """
    if outcome not in VALID_SETTLEMENT_OUTCOMES:
        raise GrantError(
            f"invalid settlement outcome {outcome!r}; "
            f"expected one of {VALID_SETTLEMENT_OUTCOMES}"
        )

    claimed_path = _grant_path(grant_id).with_suffix(".json.claimed")
    if not claimed_path.exists():
        raise GrantError(f"grant {grant_id!r} is not claimed (cannot settle)")

    try:
        with open(claimed_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise GrantError(f"claimed grant {grant_id!r} unreadable: {exc}") from exc

    if data.get("claimed_by_execution") != execution_id:
        raise GrantError(
            f"grant {grant_id!r} is claimed by execution "
            f"{data.get('claimed_by_execution')!r}, not {execution_id!r}"
        )

    settled_path = _grant_path(grant_id).with_suffix(".json.settled")
    try:
        os.rename(claimed_path, settled_path)
    except OSError as exc:
        raise GrantError(f"grant {grant_id!r} settle failed: {exc}") from exc

    data["settlement_outcome"] = outcome
    data["settled_at"] = time.time()
    with open(settled_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True, ensure_ascii=False)

    grant = DestructiveGrant.from_dict(data)
    _audit(
        {
            "event": "grant_settled",
            "grant_id": grant_id,
            "execution_id": execution_id,
            "outcome": outcome,
            "operation": grant.operation,
            "vm_id": grant.vm_id,
            "device": grant.device,
            "fs_type": grant.fs_type,
            "label": grant.label,
        }
    )
    return grant


def get_grant_state(grant_id: str) -> str:
    """Return the durable state of a grant: ``live``, ``claimed``,
    ``settled:<outcome>``, ``consumed``, ``revoked``, or ``unknown``."""
    live = _grant_path(grant_id)
    if live.exists():
        return "live"
    for suffix, label in (
        (".json.claimed", "claimed"),
        (".json.settled", "settled"),
        (".json.consumed", "consumed"),
        (".json.revoked", "revoked"),
    ):
        p = live.with_suffix(suffix)
        if p.exists():
            if label == "settled":
                try:
                    with open(p, encoding="utf-8") as fh:
                        data = json.load(fh)
                    return f"settled:{data.get('settlement_outcome', 'unknown')}"
                except (OSError, json.JSONDecodeError):
                    return "settled:unknown"
            return label
    return "unknown"


def consume_grant(grant_id: str) -> DestructiveGrant:
    """Atomically consume a grant (one-shot enforcement).

    The consumption is a rename of the grant file to a ``.consumed`` suffix,
    which is atomic on POSIX.  A second consume finds no live file and is
    denied.  Returns the consumed grant.
    """
    live = _grant_path(grant_id)
    if not live.exists():
        # Either never existed, or already consumed (replay).
        raise GrantConsumedError(f"grant {grant_id!r} not available (replay denied)")

    consumed_path = live.with_suffix(".json.consumed")
    try:
        os.rename(live, consumed_path)
    except OSError as exc:
        raise GrantError(f"grant {grant_id!r} consume failed: {exc}") from exc

    try:
        with open(consumed_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise GrantError(f"consumed grant {grant_id!r} unreadable: {exc}") from exc

    data["consumed"] = True
    data["consumed_at"] = time.time()
    with open(consumed_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True, ensure_ascii=False)

    grant = DestructiveGrant.from_dict(data)
    _audit(
        {
            "event": "grant_consumed",
            "grant_id": grant_id,
            "operation": grant.operation,
            "vm_id": grant.vm_id,
            "device": grant.device,
            "fs_type": grant.fs_type,
            "label": grant.label,
            "consumed_at": grant.consumed_at,
        }
    )
    return grant


def list_grants() -> List[DestructiveGrant]:
    """List live (non-consumed) grants, newest first."""
    d = _grants_dir()
    if not d.exists():
        return []
    grants: List[DestructiveGrant] = []
    for path in sorted(d.glob("*.json"), reverse=True):
        try:
            with open(path, encoding="utf-8") as fh:
                grants.append(DestructiveGrant.from_dict(json.load(fh)))
        except (OSError, json.JSONDecodeError):
            continue
    return grants


def revoke_grant(grant_id: str) -> bool:
    """Revoke a live grant (moves it to a ``.revoked`` suffix)."""
    live = _grant_path(grant_id)
    if not live.exists():
        return False
    revoked = live.with_suffix(".json.revoked")
    os.rename(live, revoked)
    _audit({"event": "grant_revoked", "grant_id": grant_id})
    return True
