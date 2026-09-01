# Hermes mkfs policy tests

## Baseline exécutée

```text
hardline_blocklist=246 PASS
approval_deny_rules_plus_windows=61 PASS
TOTAL_BASELINE=307 PASS
```

## Tests ajoutés (suite 8, GO Ed)

### Policy (Track C) — `tests/tools/test_governed_mkfs_policy.py`

```text
PASS
  test_valid_go_full_tuple_allows
  test_grant_file_permissions_are_0600

DENY
  test_no_grant_id_denied
  test_unknown_grant_id_denied
  test_agent_cannot_issue_grant_via_tool        (AGENT_SELF_AUTHORIZATION)
  test_grant_authorization_source_is_always_user
  test_wrong_device_denied
  test_wrong_vm_denied
  test_root_device_denied
  test_whole_disk_denied
  test_mounted_target_denied
  test_existing_filesystem_denied
  test_existing_signature_denied
  test_reuse_same_capability_denied            (replay)
  test_expired_capability_denied
  test_parameter_mutation_denied               (capability ext4, request xfs)
  test_wrong_session_denied
  test_toctou_identity_changed
  test_mounted_between_precheck_and_action
  test_nonzero_exit_denied_and_grant_not_consumed
  test_postcheck_fs_mismatch_denied
  test_audit_records_issue_verify_consume_without_secrets
  test_deny_is_audited
  test_tampered_grant_file_denied
  test_revoked_grant_denied

Sémantique PARTUUID (nouveau, suite 8)
  test_empty_gpt_partition_partuuid_only_is_not_signature
  test_real_filesystem_type_is_signature
  test_wipefs_hit_is_signature_even_without_blkid_type
```

### CLI grant — `tests/hermes_cli/test_grant_cmd.py`

Parser + issue/list/revoke/audit (6 tests).

### Registre — `tests/tools/test_governed_mkfs_registry.py`

Découverte AST du tool `governed_mkfs` (1 test).

## Track D — QGA réel sur cible disposable

```text
cible=loop0 (image 64M, type loop, non montée, sans signature)
structured_execution=PASS   (mkfs.ext4 via QGA structuré, postcheck ext4/GOVTEST/uuid)
replay=DENIED               (2e use du même grant)
arbitrary_shell_injection=FAIL/DENIED (argv allowlisté, pas de shell)
cible_nettoyee=YES          (loop détaché, image supprimée)
```

## Red line prouvée en direct

La commande pytest contenant le mot déclencheur "mkfs" a été **bloquée par la
hardline** pendant la session suite 8 — preuve que le terminal générique
reste verrouillé.

## Résultat

```text
policy_tests=32 PASS
cli_tests=6 PASS
registry_tests=1 PASS
baseline_tests=307 PASS (à re-confirmer sur suite complète)
regressions=0 (attendu)
```
