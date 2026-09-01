# HERMES_RUNTIME_RESTART_CHECKPOINT.md

Statut : reprise suite 8 après interruption WebUI (22:09:38) — session active.

## État (2026-09-01 23:02 CEST)

```text
timestamp=2026-09-01T23:02:00+02:00
source_head=3ebc4151fa (feat/destructive-explicit-approval, rebase sur origin/main 3ca096de5 OK)
runtime_head=d5281f5981 (runtime /usr/local/lib/hermes-agent, inchangé, dirty contrôlé)
webui_state=ACTIVE pid=2964610 demarre=22:09:49 imports=OK health=OK
gateway_state=ACTIVE pid=3014627 demarre=22:58:15 (restart D-Bus suite 8 §32, drain ancien OK, NRestarts=0)
runtime_files=5/5 conformes (main.py 994f50cc + 4 nouveaux matches workspace)
rollback=READY (archive runtime-admin-backups 6 snapshots + /tmp/main.py.runtime)
tests=new_tests=RUNNING (policy+registry+CLI, proc_1831bb48a422)
       baseline=307 A REPLAYER apres rebase (hardline+approval) — avant rebase 345 PASS
grant_vm148=EXPIRE (21:49:49) — re-issue requise avant usage
next_action=1) attendre tests 2) baseline 307 3) push fork + PR 4) re-issue grant 5) VM148 governed 6) Mailcow
```

## Décisions prises à la reprise

- Interruption 22:09:38 = restart volontaire WebUI (mandat §9), PAS un échec de patch :
  les 5 fichiers runtime étaient conformes, imports OK, CLI grant fonctionnel.
- Le restart gateway a été nécessaire (démarré 21:21:18, avant le correctif
  PARTUUID 21:39:12 → ancien qga_structured.py en mémoire). Fait via D-Bus
  (busctl RestartUnit), contexte extérieur, la session WebUI n'a pas été tuée.
- Réconciliation : rebase 5 commits feature (d62b5f77 docs → 4a4a6e85 PARTUUID)
  sur origin/main 3ca096de5, zéro conflit. Docs suite8 en working tree (à committer).
- RUNTIME_NEWER_THAN_WORKSPACE_BASE du mandat = factuellement FAUX (runtime
  d5281f59 est plus vieux que la base workspace 894fc35337) — sans impact :
  patch minimal appliqué au runtime, cohérent, rollback prêt.

## Preuves

- Hardline terminal prouvée vivante à 22:59 (BLOCKED sur commande contenant le mot interdit).
- CLI `hermes grant issue/list/revoke/audit` fonctionne (help + list OK à 22:50).
- Registre : imports tools.destructive_grants/qga_structured/governed_mkfs_tool OK.

## Rollback si besoin

- Restaurer depuis /home/edou/admin/srv-hermes-normalization/archive/runtime-admin-backups/
- ou /tmp/main.py.runtime (main.py pré-patch, sha != 994f50cc)
- Ne PAS ré-exécuter le patch (8 fichiers déjà conformes).
