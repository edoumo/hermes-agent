# HERMES_TRUSTED_ONE_SHOT_CAPABILITY.md

Statut : **QUALIFIED** (suite 8, GO Ed 2026-09-01)

## Principe

Une capacité destructrice one-shot, courte, liée à un tuple exact, émise
exclusivement à la frontière utilisateur (`hermes grant issue`), consommée
atomiquement après succès, avec anti-replay et audit.

## Frontière de confiance

- **Émission** : CLI `hermes grant issue` — exécutée par l'utilisateur (Ed)
  dans un terminal de confiance. Aucun outil modèle ne peut émettre un grant
  (test `test_agent_cannot_issue_grant_via_tool`).
- **Consommation** : outil modèle `governed_mkfs` — vérifie le tuple exact,
  les prechecks fail-closed, le TOCTOU, exécute via QGA structuré, postcheck,
  puis consomme atomiquement.
- **Session binding** : le grant est lié à `session_id` ; le handler reçoit
  `session_id` du dispatch et le compare.

## Structure d'un grant

```text
grant_id            uuid4 (opaque, seul identifiant visible du modèle)
operation           CREATE_FILESYSTEM
vm_id               148
hostname            hp-mail
device              /dev/sdb1
fs_type             ext4
label               MAILCOW_DOCKER
authorization_source USER (jamais AGENT)
authorization_subject Ed
session_id          c4be704a4355
issued_at / expires_at   TTL 600s (max 3600)
nonce               secret interne (jamais dans l'audit)
binding_sha256      hash d'intégrité du tuple (public)
consumed / consumed_at
```

## Garanties

| Propriété | Mécanisme | Test |
|---|---|---|
| single-use | `consume_grant` atomique (fichier 0600, verrou) | `test_reuse_same_capability_denied` |
| short-lived | `expires_at` rejeté si dépassé | `test_expired_capability_denied` |
| scope-bound | tuple exact vérifié à chaque use | `test_wrong_device_denied`, `test_wrong_vm_denied` |
| non-transferable | lié à `session_id` + `authorization_subject` | `test_wrong_session_denied` |
| non-replayable | consommation atomique + nonce | `test_reuse_same_capability_denied` |
| auditable | `read_audit_trail()` sans secrets | `test_audit_records_issue_verify_consume_without_secrets` |
| anti-tampering | `binding_sha256` vérifié au load | `test_tampered_grant_file_denied` |
| révocable | `hermes grant revoke` | `test_revoked_grant_denied` |

## Red lines

- `AGENT_SELF_AUTHORIZATION=NO` : aucun chemin modèle ne peut émettre un grant.
- `CAPABILITY_REPLAY=NO` : un grant consommé est définitivement mort.
- `CAPABILITY_PERMANENT=NO` : TTL max 3600s, jamais de grant permanent.
- `WILDCARD_DEVICE=NO` : `/dev/sd*` rejeté par validation.

## Fichiers

- `tools/destructive_grants.py` — store, verify, consume, audit, revoke.
- `hermes_cli/subcommands/grant.py` — frontière utilisateur (issue/list/revoke/audit).
- `hermes_cli/main.py` — dispatch `hermes grant <subcmd>`.
