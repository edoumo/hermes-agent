# HERMES_STRUCTURED_QGA.md

Statut : **QUALIFIED** (suite 8, GO Ed 2026-09-01)

## Principe

QGA (`qm guest exec`) est un canal puissant. Il n'est jamais exposé au modèle
pour des actions destructives. Un adaptateur structuré construit l'argv
exclusivement à partir de champs allowlistés validés.

## API

```text
qga_guest_identity(vm_id)            -> hostname + boot_id (read-only)
qga_prechecks(vm_id, device)         -> tous les checks §13 (read-only)
qga_create_filesystem(vm_id, device, fs_type, label)
                                     -> argv = [binary_allowlisté, "-L", label, device]
qga_postcheck(vm_id, device, fs_type, label)
                                     -> TYPE/LABEL/UUID via blkid -o export
```

## Construction de commande (§15)

```text
fs_type=ext4  ->  /usr/sbin/mkfs.ext4   (table FS_TYPE_TO_BINARY, allowlist stricte)
fs_type=xfs   ->  /usr/sbin/mkfs.xfs
label         ->  regex ^[A-Za-z0-9_.-]{1,16}$  (validée avant usage)
device        ->  ^/dev/(sd[a-z][0-9]+|vd[a-z][0-9]+|nvme[0-9]+n[0-9]+p[0-9]+|loop[0-9]+)$
                 (partition ou loop uniquement — jamais un disque entier, jamais root)
```

Aucun shell, aucune interpolation non contrôlée, aucun `bash -c` avec texte
modèle. Le seul `bash -lc` du module est un check Docker read-only avec
`shlex.quote(device)`.

## Prechecks (§13) — fail-closed

`device_exists`, `is_block_device`, `mounted`, `swap`, `filesystem_existing`,
`filesystem_signature`, `lvm_member`, `mdraid_member`, `holders`, `docker_use`,
`fstab_use`. Tout UNKNOWN/AMBIGUOUS/FAIL -> DENY.

### Sémantique PARTUUID (corrigé suite 8)

`blkid` retourne rc=0 avec `PARTUUID=` pour **toute** partition GPT, même
vide. PARTUUID est l'identité de la partition dans la table, pas une signature
de données. Seul un `TYPE=` (filesystem réel) ou un hit `wipefs -n` compte
comme signature. Tests : `TestPrecheckPartuuidSemantics` (3 cas).

## TOCTOU (§14)

Le handler exécute `qga_prechecks` deux fois (precheck puis recheck immédiat
avant action) et compare l'identité critique (major:minor, size, mounted,
filesystem state, holders). Toute différence -> DENY.

## Transport

- SSH `hote1`/`192.168.1.10` -> `qm guest exec <vm> -- <argv>`.
- Parseur du résultat : `{"exitcode": N, "out-data": ..., "err-data": ...}`
  au niveau racine (corrigé suite 8 — le format réel n'a pas de wrapper
  `return`).
- Clé SSH : `DEFAULT_QGA_SSH_KEY` (check_fn du tool).

## Tests

- `TestPrecheckPartuuidSemantics` — sémantique signature (3 cas).
- Track D réel : loop device 64M sur VM148, exécution structurée PASS,
  replay DENIED, cible nettoyée.
