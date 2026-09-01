# Proposed Hermes destructive action policy

## Status

`DESIGN_PROPOSED_NOT_APPROVED`

Cette doctrine ne doit pas être implémentée sans le GO architectural séparé prévu par le mandat.

## Classes

| Classe | Sémantique | Exemple |
|---|---|---|
| `SAFE` | exécution normale | lecture d'état |
| `SENSITIVE` | approval standard, potentiellement persistable | mutation récupérable |
| `DESTRUCTIVE_EXPLICIT_APPROVAL` | capacité utilisateur one-shot, tuple exact, préchecks fail-closed, audit obligatoire | création d'un FS neuf |
| `ABSOLUTE_DENY` | aucune approbation agent | root/system wipe, fork bomb |

## Invariants de `DESTRUCTIVE_EXPLICIT_APPROVAL`

1. Le GO est matérialisé par un reçu de capacité créé à une frontière utilisateur de confiance, jamais par le modèle.
2. Le reçu est lié à `session/user/operation/host-or-VM/device/filesystem/label`.
3. Il est one-shot, à durée courte et consommé atomiquement.
4. Le moteur parse la commande réelle et compare tous les champs au reçu.
5. Les préchecks sont exécutés par le cœur Hermes via le même canal que l'action.
6. Toute valeur `FALSE`, `UNKNOWN` ou `AMBIGUOUS` refuse l'action.
7. Les identités `major:minor`, taille, parent, mount state et signatures sont relues immédiatement avant exécution.
8. `--yolo`, mode off, smart approval, allowlist, cron approve et `force=True` ne peuvent jamais autoriser cette classe.
9. L'audit enregistre décision, prechecks, canal, exit code et postchecks, sans secret.

## Workflow formatage neuf

```text
trusted grant exists
AND tuple exact
AND block device exists
AND target != root/root parent
AND not mounted/swap/PV/mdraid/active crypt
AND no holders
AND no filesystem/signature
AND not used by Docker
AND identity/size/parent match
AND immediate recheck unchanged
=> execute once + consume grant + postcheck + audit
```

Tout autre chemin reste hardline.

## Classification actuelle

### Rester `ABSOLUTE_DENY`

- suppression récursive de `/`, roots système ou home ;
- écriture brute `dd`/redirection vers block device dans cette première évolution ;
- fork bomb ;
- kill de tous les processus ;
- shutdown/reboot/init/telinit/systemctl poweroff dans le périmètre de ce mandat.

### Candidat uniquement après architecture approuvée

- `mkfs*` sur nouvelle cible vide et exactement autorisée.

### Hors scope, à auditer plus tard sans autorisation implicite

`wipefs`, `parted`, `fdisk`, `pvcreate`, `vgremove`, `lvremove`, `cryptsetup`, `zpool`, `mdadm` ne figurent pas dans la hardline POSIX inventoriée. Le présent mandat n'autorise pas leur reclassement. Leur absence de l'ABSOLUTE_DENY doit faire l'objet d'une mission séparée.

## Trust boundary recommandé

Le composant d'entrée utilisateur (WebUI/gateway/CLI) doit créer un `DestructiveGrant` signé ou stocké côté hôte, inaccessible aux outils génériques. Le modèle ne reçoit qu'un identifiant opaque. `terminal_tool` charge le reçu, valide et consomme la capacité. Pour QGA, un transport policy-aware doit exposer `node/vmid/guest-command` au moteur au lieu d'un helper shell opaque.
