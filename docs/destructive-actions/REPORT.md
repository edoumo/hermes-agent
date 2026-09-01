# REPORT — CI/CD suite8 — Hermes Trusted One-Shot Capability + Governed MKFS + Structured QGA

## Verdict

`HERMES_GOVERNED_DESTRUCTIVE_CAPABILITY_QUALIFIED` (en cours — VM148 et Mailcow suivent)

## GO Ed

Mandat suite 8 (attachment 2026-09-01) : GO explicite pour
`TRUSTED_ONE_SHOT_CAPABILITY + GOVERNED_MKFS + STRUCTURED_QGA`, déploiement
rollbackable, restart Hermes autorisé, VM148 `/dev/sdb1` préautorisé (§29),
reprise Mailcow automatique (§34).

## Réalisé (Track B-F)

- `tools/destructive_grants.py` — store one-shot (issue/verify/consume/revoke/audit,
  fichier 0600, binding SHA256, nonce, TTL max 3600s, session binding).
- `hermes_cli/subcommands/grant.py` + `hermes_cli/main.py` — frontière
  utilisateur `hermes grant issue/list/revoke/audit` (CLI uniquement).
- `tools/governed_mkfs_tool.py` — outil modèle `governed_mkfs` (verify ->
  prechecks §13 fail-closed -> TOCTOU recheck -> exec structurée -> postcheck
  -> consume atomique).
- `tools/qga_structured.py` — adaptateur QGA structuré (argv allowlisté,
  parseur corrigé, sémantique PARTUUID corrigée).
- Tests : 32 policy + 6 CLI + 1 registre = 39 nouveaux.
- Track D réel : loop device 64M sur VM148, exécution structurée PASS,
  replay DENIED, cible nettoyée.
- Déploiement runtime via `hermes-admin` (backups + manifest), restart
  gateway + WebUI, registre vérifié (93 tools, `governed_mkfs` exposé).
- Branche poussée : `feat/destructive-explicit-approval` (3 commits).

## Correctif PARTUUID (découvert en requalification)

`blkid` retourne rc=0 avec `PARTUUID=` pour toute partition GPT même vide —
identité de table, pas signature de données. Corrigé (seul `TYPE=` ou hit
`wipefs` compte), testé (3 cas), déployé sur runtime, requalification VM148
verte.

## Requalification VM148 (§30)

```text
target_identity=PASS (hp-mail, boot_id 2fb95573…)
device_exists=YES  is_block_device=YES  major_minor=8:17
size=137438953472 (128G)  parent=sdb1
mounted=NO  swap=NO  filesystem_existing=NO  filesystem_signature=NO
lvm_member=NO  mdraid_member=NO  holders=NONE  docker_use=NO  fstab_use=NO
```

## État final

```text
status=HERMES_GOVERNED_DESTRUCTIVE_CAPABILITY_QUALIFIED
terminal_policy_weakened=NO
trusted_one_shot_capability=PASS
go_origin_binding=PASS
target_binding=PASS
replay_protection=PASS
structured_qga=PASS
governed_mkfs=PASS
regression_tests=PENDING (suite complète en cours)
runtime_rollback=READY
vm148_mkfs=PENDING (gate regression_tests)
mailcow_resumed=NO
```
