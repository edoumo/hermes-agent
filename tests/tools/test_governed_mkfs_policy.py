"""Policy tests for the governed destructive capability (mandate §20).

Covers every scenario the mandate requires:

PASS
    valid user GO + correct VM + correct device + empty target + all
    prechecks PASS -> capability issued -> filesystem creation allowed

DENY
    no explicit user authorization
    agent self-authorization (structurally impossible: no model tool can
    issue a grant; the issue path is CLI-only)
    different device
    different VM
    root device
    mounted target
    existing filesystem
    existing signature
    capability replay
    expired capability
    parameter mutation (capability says ext4, request says xfs)

The QGA transport is mocked at the ``tools.qga_structured`` boundary so the
policy engine is exercised end-to-end without touching a real VM.  The
structured-QGA adapter itself is tested separately (Track D) against a real
disposable loop device.
"""

import json
import time
import uuid
from unittest.mock import patch

import pytest

from tools import destructive_grants as dg
from tools.governed_mkfs_tool import _handle_governed_mkfs

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _issue(
    *,
    device="/dev/sdb1",
    fs_type="ext4",
    label="MAILCOW_DOCKER",
    vm_id="148",
    hostname="hp-mail",
    subject="Ed",
    session_id="sess-1",
    ttl=600,
):
    from tools.grant_authority import HumanApprovalReceipt, _store_receipt

    now = time.time()
    receipt = _store_receipt(
        HumanApprovalReceipt(
            receipt_id="rcpt-" + "1" * 28,
            request_id="req-11111111111111111111111111111111",
            request_digest="d" * 64,
            session_id=session_id,
            turn_id="turn-1",
            tool_call_id="tool-1",
            operation="CREATE_FILESYSTEM",
            vm_id=vm_id,
            device=device,
            fs_type=fs_type,
            label=label,
            issued_at=now,
            expires_at=now + ttl,
        )
    )
    return dg.issue_grant(
        operation="CREATE_FILESYSTEM",
        vm_id=vm_id,
        hostname=hostname,
        device=device,
        fs_type=fs_type,
        label=label,
        authorization_subject=subject,
        session_id=session_id,
        receipt_id=receipt.receipt_id,
        incarnation_product_uuid="uuid-A",
        incarnation_boot_id="boot-A",
        incarnation_hostname=hostname,
        ttl_seconds=ttl,
    )


def _make_receipt(
    *,
    operation="CREATE_FILESYSTEM",
    vm_id="148",
    device="/dev/sdb1",
    fs_type="ext4",
    label="MAILCOW_DOCKER",
    session_id="sess-1",
    ttl=600,
):
    """Mint a correlated human approval receipt in the process-local store."""
    from tools.grant_authority import HumanApprovalReceipt, _store_receipt

    now = time.time()
    return _store_receipt(
        HumanApprovalReceipt(
            receipt_id="rcpt-" + uuid.uuid4().hex,
            request_id="req-11111111111111111111111111111111",
            request_digest="d" * 64,
            session_id=session_id,
            turn_id="turn-1",
            tool_call_id="tool-1",
            operation=operation,
            vm_id=vm_id,
            device=device,
            fs_type=fs_type,
            label=label,
            issued_at=now,
            expires_at=now + ttl,
        )
    )


def _clean_prechecks(device="/dev/sdb1"):
    """A fully green precheck payload (mandate §13 all PASS)."""
    return {
        "vm_id": "148",
        "device": device,
        "device_exists": True,
        "is_block_device": True,
        "major_minor": "8:17",
        "size_bytes": 137438953472,
        "parent": "sdb",
        "mounted": False,
        "swap": False,
        "filesystem_existing": False,
        "filesystem_signature": False,
        "lvm_member": False,
        "mdraid_member": False,
        "holders": [],
        "docker_use": False,
        "fstab_use": False,
    }


def _exec_ok(device="/dev/sdb1", fs_type="ext4", label="MAILCOW_DOCKER"):
    return {
        "operation": "CREATE_FILESYSTEM",
        "vm_id": "148",
        "device": device,
        "fs_type": fs_type,
        "label": label,
        "guest_argv": [dg.FS_TYPE_TO_BINARY[fs_type], "-L", label, device],
        "exit_code": 0,
        "out_data": "",
        "err_data": "",
    }


def _postcheck_ok(fs_type="ext4", label="MAILCOW_DOCKER"):
    return {
        "exit_code": 0,
        "filesystem": fs_type,
        "label": label,
        "uuid": "11111111-2222-3333-4444-555555555555",
    }


def _call(grant_id, *, device="/dev/sdb1", fs_type="ext4", label="MAILCOW_DOCKER",
          vm_id="148", session_id="sess-1"):
    return json.loads(
        _handle_governed_mkfs(
            {
                "grant_id": grant_id,
                "vm_id": vm_id,
                "device": device,
                "fs_type": fs_type,
                "label": label,
            },
            session_id=session_id,
        )
    )


def _mock_qga(prechecks=None, recheck=None, exec_result=None, postcheck=None,
              identity=None):
    """Patch the structured QGA boundary with deterministic payloads.

    ``qga_prechecks`` is called twice by the tool (precheck then TOCTOU
    recheck); when ``recheck`` is provided it is returned on the second
    call, otherwise the same payload is returned both times.  ``identity``
    is the incarnation payload returned by ``qga_guest_identity`` (defaults
    to the grant's own incarnation).
    """
    prechecks = prechecks if prechecks is not None else _clean_prechecks()
    recheck = recheck if recheck is not None else prechecks
    exec_result = exec_result if exec_result is not None else _exec_ok()
    postcheck = postcheck if postcheck is not None else _postcheck_ok()
    identity = identity if identity is not None else {
        "hostname": "hp-mail", "boot_id": "boot-A", "product_uuid": "uuid-A",
    }

    calls = {"n": 0}

    def _prechecks(vm_id, device):
        calls["n"] += 1
        return recheck if calls["n"] >= 2 else prechecks

    return patch.multiple(
        "tools.governed_mkfs_tool",
        qga_prechecks=_prechecks,
        qga_guest_identity=lambda vm_id: dict(identity),
        qga_create_filesystem=lambda vm_id, device, fs_type, label: exec_result,
        qga_postcheck=lambda vm_id, device, fs_type, label: postcheck,
    )


# ---------------------------------------------------------------------------
# PASS
# ---------------------------------------------------------------------------


class TestPass:
    def test_valid_go_full_tuple_allows(self):
        grant = _issue()
        with _mock_qga():
            result = _call(grant.grant_id)
        assert result["decision"] == "ALLOW"
        assert result["capability_consumed"] is True
        assert result["fs_type"] == "ext4"
        assert result["label"] == "MAILCOW_DOCKER"
        assert result["uuid"] == "11111111-2222-3333-4444-555555555555"
        # The grant is now consumed.
        with pytest.raises(dg.GrantConsumedError):
            dg.load_grant(grant.grant_id)

    def test_grant_file_permissions_are_0600(self):
        grant = _issue()
        path = dg._grant_path(grant.grant_id)
        assert (path.stat().st_mode & 0o777) == 0o600
        # Directory is 0700.
        assert (path.parent.stat().st_mode & 0o777) == 0o700


# ---------------------------------------------------------------------------
# DENY
# ---------------------------------------------------------------------------


class TestDenyNoGo:
    def test_no_grant_id_denied(self):
        with _mock_qga():
            result = _call("")
        assert result["decision"] == "DENY"
        assert result["reason"] == "grant_id_required"

    def test_unknown_grant_id_denied(self):
        with _mock_qga():
            result = _call("00000000-0000-0000-0000-000000000000")
        assert result["decision"] == "DENY"
        assert result["reason"] == "grant_denied"


class TestDenyAgentSelfAuthorization:
    def test_agent_cannot_issue_grant_via_tool(self):
        """The issue path is CLI-only: no model tool imports it.

        Assert that the governed tool module does not expose any issue
        capability and that the grants module is not importable from any
        registered tool handler path.
        """
        import tools.governed_mkfs_tool as gmt

        assert not hasattr(gmt, "issue_grant")
        # The handler only references verify/consume, never issue.
        import inspect

        src = inspect.getsource(gmt._handle_governed_mkfs)
        assert "issue_grant" not in src
        assert "authorization_source" not in src

    def test_grant_authorization_source_is_always_user(self):
        grant = _issue()
        assert grant.authorization_source == "USER"
        assert grant.authorization_subject == "Ed"


class TestDenyWrongDevice:
    def test_authorized_sdb1_requested_sdc1(self):
        grant = _issue(device="/dev/sdb1")
        with _mock_qga():
            result = _call(grant.grant_id, device="/dev/sdc1")
        assert result["decision"] == "DENY"
        assert result["reason"] == "grant_denied"
        # Grant must be settled failed_pre_effect (never returns to the pool).
        assert dg.get_grant_state(grant.grant_id) == "settled:failed_pre_effect"

    def test_authorized_sdb1_requested_sdb(self):
        """Whole disk is a different target than the authorized partition."""
        grant = _issue(device="/dev/sdb1")
        with _mock_qga():
            result = _call(grant.grant_id, device="/dev/sdb")
        assert result["decision"] == "DENY"


class TestDenyWrongVm:
    def test_authorized_148_requested_149(self):
        grant = _issue(vm_id="148")
        with _mock_qga():
            result = _call(grant.grant_id, vm_id="149")
        assert result["decision"] == "DENY"
        assert result["reason"] == "grant_denied"


class TestDenyRootDevice:
    def test_root_device_never_issuable(self):
        with pytest.raises(dg.GrantError):
            _issue(device="/dev/sda1")
        with pytest.raises(dg.GrantError):
            _issue(device="/dev/sda")
        with pytest.raises(dg.GrantError):
            _issue(device="/dev/vda1")
        with pytest.raises(dg.GrantError):
            _issue(device="/dev/nvme0n1")

    def test_root_device_request_denied(self):
        grant = _issue(device="/dev/sdb1")
        with _mock_qga():
            result = _call(grant.grant_id, device="/dev/sda1")
        assert result["decision"] == "DENY"


class TestDenyMounted:
    def test_mounted_target_denied(self):
        grant = _issue()
        prechecks = _clean_prechecks()
        prechecks["mounted"] = True
        prechecks["mount_target"] = "/var/lib/docker"
        with _mock_qga(prechecks=prechecks):
            result = _call(grant.grant_id)
        assert result["decision"] == "DENY"
        assert result["reason"] == "prechecks_failed"
        assert "mounted=YES" in result["failures"]


class TestDenyExistingFilesystem:
    def test_existing_fs_denied(self):
        grant = _issue()
        prechecks = _clean_prechecks()
        prechecks["filesystem_existing"] = True
        with _mock_qga(prechecks=prechecks):
            result = _call(grant.grant_id)
        assert result["decision"] == "DENY"
        assert "filesystem_existing=YES" in result["failures"]


class TestDenyExistingSignature:
    def test_existing_signature_denied(self):
        grant = _issue()
        prechecks = _clean_prechecks()
        prechecks["filesystem_signature"] = True
        with _mock_qga(prechecks=prechecks):
            result = _call(grant.grant_id)
        assert result["decision"] == "DENY"
        assert "filesystem_signature=YES" in result["failures"]


class TestPrecheckPartuuidSemantics:
    """blkid returns rc=0 with PARTUUID= for ANY GPT partition, even empty.

    PARTUUID is partition-table identity, not a data signature.  Only a real
    filesystem TYPE= (or a wipefs hit) must set filesystem_signature.
    """

    def _run_prechecks(self, blkid_out, wipefs_out=""):
        import tools.qga_structured as qs

        def _fake_exec(vm_id, argv, timeout=None):
            cmd = argv[0]
            if cmd == "lsblk":
                out = json.dumps({
                    "blockdevices": [{
                        "name": "sdb1", "type": "part", "maj:min": "8:17",
                        "size": "128G", "mountpoints": [], "fstype": None,
                    }]
                })
                return {"exit_code": 0, "out_data": out, "err_data": ""}
            if cmd == "findmnt":
                return {"exit_code": 1, "out_data": "", "err_data": ""}
            if cmd == "blkid":
                return {"exit_code": 0, "out_data": blkid_out, "err_data": ""}
            if cmd == "wipefs":
                return {"exit_code": 0, "out_data": wipefs_out, "err_data": ""}
            if cmd == "grep" and "swaps" in argv:
                return {"exit_code": 1, "out_data": "", "err_data": ""}
            if cmd == "pvs":
                return {"exit_code": 1, "out_data": "", "err_data": ""}
            if cmd == "cat":
                return {"exit_code": 0, "out_data": "", "err_data": ""}
            if cmd == "ls":
                return {"exit_code": 0, "out_data": "", "err_data": ""}
            if cmd == "bash":
                return {"exit_code": 0, "out_data": "", "err_data": ""}
            return {"exit_code": 0, "out_data": "", "err_data": ""}

        with patch.object(qs, "_qm_guest_exec", side_effect=_fake_exec):
            return qs.qga_prechecks("148", "/dev/sdb1")

    def test_empty_gpt_partition_partuuid_only_is_not_signature(self):
        checks = self._run_prechecks(
            blkid_out='/dev/sdb1: PARTUUID="c675ed5f-8323-45a3-b8d0-4e62097a1a09"'
        )
        assert checks["filesystem_signature"] is False
        assert checks["device_exists"] is True
        assert checks["is_block_device"] is True

    def test_real_filesystem_type_is_signature(self):
        checks = self._run_prechecks(
            blkid_out='/dev/sdb1: UUID="abc" TYPE="ext4" PARTUUID="c675ed5f"'
        )
        assert checks["filesystem_signature"] is True

    def test_wipefs_hit_is_signature_even_without_blkid_type(self):
        checks = self._run_prechecks(
            blkid_out='',
            wipefs_out="DEVICE OFFSET TYPE UUID LABEL\nsdb1 0x438 ext4",
        )
        assert checks["filesystem_signature"] is True


class TestDenyReplay:
    def test_reuse_same_capability_denied(self):
        grant = _issue()
        with _mock_qga():
            first = _call(grant.grant_id)
        assert first["decision"] == "ALLOW"
        # Second use of the same grant: replay -> DENY.
        with _mock_qga():
            second = _call(grant.grant_id)
        assert second["decision"] == "DENY"
        assert second["reason"] == "grant_denied"
        assert "replay" in second["detail"].lower() or "consumed" in second["detail"].lower()


class TestDenyExpired:
    def test_expired_capability_denied(self):
        grant = _issue(ttl=1)
        time.sleep(1.1)
        with _mock_qga():
            result = _call(grant.grant_id)
        assert result["decision"] == "DENY"
        assert result["reason"] == "grant_denied"
        assert "expired" in result["detail"].lower()


class TestDenyParameterMutation:
    def test_capability_ext4_request_xfs(self):
        grant = _issue(fs_type="ext4")
        with _mock_qga():
            result = _call(grant.grant_id, fs_type="xfs")
        assert result["decision"] == "DENY"
        assert result["reason"] == "grant_denied"

    def test_capability_label_a_request_label_b(self):
        grant = _issue(label="MAILCOW_DOCKER")
        with _mock_qga():
            result = _call(grant.grant_id, label="OTHER_LABEL")
        assert result["decision"] == "DENY"

    def test_capability_operation_fixed(self):
        """The tool only ever requests CREATE_FILESYSTEM; the grant is bound
        to that operation at issue time."""
        grant = _issue()
        assert grant.operation == "CREATE_FILESYSTEM"
        with pytest.raises(dg.GrantError):
            dg.issue_grant(
                operation="WIPEFS",
                vm_id="148", hostname="hp-mail", device="/dev/sdb1",
                fs_type="ext4", label="X",
                authorization_subject="Ed", session_id="sess-1",
                receipt_id=_make_receipt().receipt_id,
                incarnation_product_uuid="uuid-A",
                incarnation_boot_id="boot-A",
                incarnation_hostname="hp-mail",
            )


class TestDenySessionBinding:
    def test_different_session_denied(self):
        grant = _issue(session_id="sess-1")
        with _mock_qga():
            result = _call(grant.grant_id, session_id="sess-2")
        assert result["decision"] == "DENY"
        assert result["reason"] == "grant_denied"

    def test_missing_session_denied(self):
        grant = _issue()
        with _mock_qga():
            result = _call(grant.grant_id, session_id="")
        assert result["decision"] == "DENY"
        assert result["reason"] == "session_id_required"


class TestDenyToctou:
    def test_identity_changed_between_precheck_and_action(self):
        grant = _issue()
        prechecks = _clean_prechecks()
        recheck = _clean_prechecks()
        recheck["major_minor"] = "8:18"  # device identity changed
        with _mock_qga(prechecks=prechecks, recheck=recheck):
            result = _call(grant.grant_id)
        assert result["decision"] == "DENY"
        assert result["reason"] == "toctou_identity_changed"

    def test_mounted_between_precheck_and_action(self):
        grant = _issue()
        prechecks = _clean_prechecks()
        recheck = _clean_prechecks()
        recheck["mounted"] = True
        with _mock_qga(prechecks=prechecks, recheck=recheck):
            result = _call(grant.grant_id)
        assert result["decision"] == "DENY"
        assert result["reason"] == "toctou_recheck_failed"


class TestDenyExecutionFailure:
    def test_nonzero_exit_indeterminate_and_grant_settled(self):
        grant = _issue()
        exec_result = _exec_ok()
        exec_result["exit_code"] = 1
        exec_result["err_data"] = "mkfs.ext4: Device or resource busy"
        with _mock_qga(exec_result=exec_result):
            result = _call(grant.grant_id)
        assert result["decision"] == "INDETERMINATE"
        assert result["reason"] == "execution_exit_nonzero"
        assert result["outcome"] == "indeterminate"
        # A mutation may have happened: the grant is settled indeterminate and
        # NEVER returns to the pool (no blind retry).
        assert dg.get_grant_state(grant.grant_id) == "settled:indeterminate"
        with pytest.raises(dg.GrantConsumedError):
            dg.load_grant(grant.grant_id)

    def test_postcheck_fs_mismatch_indeterminate(self):
        grant = _issue()
        with _mock_qga(postcheck=_postcheck_ok(fs_type="xfs")):
            result = _call(grant.grant_id)
        assert result["decision"] == "INDETERMINATE"
        assert result["reason"] == "postcheck_fs_mismatch"
        assert result["outcome"] == "indeterminate"


class TestAuditTrail:
    def test_audit_records_issue_verify_consume_without_secrets(self):
        grant = _issue()
        with _mock_qga():
            _call(grant.grant_id)
        entries = dg.read_audit_trail()
        events = [e["event"] for e in entries]
        assert "grant_issued" in events
        assert "grant_claimed" in events
        assert "grant_verified" in events
        assert "grant_settled" in events
        # No secrets in the trail (nonce is the only secret-bearing field;
        # binding_sha256 is a public integrity hash and is expected).
        blob = json.dumps(entries)
        assert "nonce" not in blob

    def test_deny_is_audited(self):
        grant = _issue(device="/dev/sdb1")
        with _mock_qga():
            _call(grant.grant_id, device="/dev/sdc1")
        entries = dg.read_audit_trail()
        assert any(e["event"] == "grant_denied" and e["reason"] == "tuple_mismatch"
                   for e in entries)


class TestGrantIntegrity:
    def test_tampered_grant_file_denied(self):
        grant = _issue()
        path = dg._grant_path(grant.grant_id)
        data = json.loads(path.read_text())
        data["device"] = "/dev/sdc1"
        path.write_text(json.dumps(data))
        with _mock_qga():
            result = _call(grant.grant_id)
        assert result["decision"] == "DENY"
        assert result["reason"] == "grant_denied"
        assert "integrity" in result["detail"].lower() or "tampered" in result["detail"].lower()

    def test_revoked_grant_denied(self):
        grant = _issue()
        assert dg.revoke_grant(grant.grant_id) is True
        with _mock_qga():
            result = _call(grant.grant_id)
        assert result["decision"] == "DENY"
