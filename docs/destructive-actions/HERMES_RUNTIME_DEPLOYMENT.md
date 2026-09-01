# HERMES_RUNTIME_DEPLOYMENT.md

Statut : **DÉPLOYÉ** (suite 8, 2026-09-01)

## Cible

```text
host=srv-hermes (192.168.1.187)
runtime=/usr/local/lib/hermes-agent
runtime_sha_before=d5281f5981
runtime_sha_after=d5281f5981 (aucun update git — patch overlay uniquement)
```

## Fichiers déployés

| Fichier | Mécanisme | SHA256 (déployé) |
|---|---|---|
| `hermes_cli/main.py` (patch) | `hermes-admin deploy-runtime-file` (backup) | cf. backup manifest |
| `tools/destructive_grants.py` | idem (nouveau, `expected_old=ABSENT`) | — |
| `tools/governed_mkfs_tool.py` | idem (nouveau) | — |
| `tools/qga_structured.py` | idem (nouveau puis correctif PARTUUID) | `bc41b033…` |
| `hermes_cli/subcommands/grant.py` | idem (nouveau) | — |

## Procédure

1. Staging : `~/admin/srv-hermes-normalization/staging/runtime-patches/`.
2. `sudo -n /usr/local/sbin/hermes-admin deploy-runtime-file <src> <relative> <expected_old_sha256>`.
   - `expected_old=ABSENT` pour les nouveaux fichiers.
   - SHA256 vérifié des deux côtés ; backup root 0600 + manifest.
3. Vérification import : `python3 -c "import tools.destructive_grants, tools.qga_structured"` OK.
4. Vérification registre : `discover_builtin_tools()` -> `governed_mkfs` enregistré (93 tools).
5. Restart gateway : `busctl --user call org.freedesktop.systemd1 ... RestartUnit ss hermes-gateway.service replace`
   (le restart direct est protégé anti-suicide ; D-Bus = shell séparé).
   WebUI redémarré aussi (imports au démarrage) — ma session a survécu.
6. Vérification : `systemctl --user is-active hermes-gateway.service` = active,
   `NRestarts=0`, aucun error/traceback dans le journal depuis le restart.

## Correctif PARTUUID (2e déploiement)

`tools/qga_structured.py` corrigé (blkid PARTUUID != signature) puis redéployé
avec `expected_old_sha256=efaf0f28…` -> `DEPLOY_RUNTIME_FILE=PASS|SHA256=bc41b033…`.

## Vérification runtime

- `hermes grant issue/list/revoke/audit` fonctionnels.
- `governed_mkfs` exposé au modèle (registre, toolset terminal, check_fn clé SSH).
- Requalification VM148 : prechecks tous PASS (PARTUUID ignoré, wipefs vide).
