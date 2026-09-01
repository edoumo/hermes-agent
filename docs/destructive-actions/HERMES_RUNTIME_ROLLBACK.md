# HERMES_RUNTIME_ROLLBACK.md

Statut : **READY** (suite 8, 2026-09-01)

## État capturé avant déploiement

```text
runtime_version_before=d5281f5981
runtime_sha_before=d5281f5981
config_before=inchangée (aucune modification config.yaml)
service_state_before=hermes-gateway active (depuis 2026-08-24 23:45:14)
service_state_before=hermes-webui active
rollback_ready=YES
```

## Backups

`~/admin/srv-hermes-normalization/archive/runtime-admin-backups/` :

- `20260901T193912Z/` — backup de `tools/qga_structured.py` (version pré-correctif,
  SHA `efaf0f28…`) + manifest.
- Backups des 4 nouveaux fichiers + `main.py` patché (mêmes stamps, manifest).

## Procédure de rollback

1. **Nouveaux fichiers** (suppression) :
   ```bash
   sudo -n /usr/local/sbin/hermes-admin rollback-runtime-file <backup_dir> tools/destructive_grants.py <sha_deploye>
   sudo -n /usr/local/sbin/hermes-admin rollback-runtime-file <backup_dir> tools/governed_mkfs_tool.py <sha_deploye>
   sudo -n /usr/local/sbin/hermes-admin rollback-runtime-file <backup_dir> tools/qga_structured.py <sha_deploye>
   sudo -n /usr/local/sbin/hermes-admin rollback-runtime-file <backup_dir> hermes_cli/subcommands/grant.py <sha_deploye>
   ```
   -> `RESTORED=ABSENT` (fichier supprimé).

2. **main.py** (restauration) :
   ```bash
   sudo -n /usr/local/sbin/hermes-admin rollback-runtime-file <backup_dir> hermes_cli/main.py <sha_patche>
   ```
   -> restaure la version runtime d'origine.

3. **Restart gateway + WebUI** (même procédure D-Bus que le déploiement).

4. **Vérification** :
   ```bash
   systemctl --user is-active hermes-gateway.service   # active
   hermes grant list                                    # erreur = rollback OK (sous-commande absente)
   python3 -c "import tools.destructive_grants"         # ImportError = rollback OK
   ```

## Garanties

- Chaque rollback vérifie le SHA256 courant avant restauration (mismatch -> stop).
- Les backups sont root:root 0600, jamais touchés par le runtime.
- Aucune modification de config.yaml, aucun changement de service systemd
  (seuls des restarts), donc rollback sans résidu de configuration.
