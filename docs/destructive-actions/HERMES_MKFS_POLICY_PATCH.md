# Hermes mkfs policy patch decision

## Patch status

```text
PATCH_STATUS=APPLIED
CODE_CHANGE=YES
RUNTIME_CHANGE=YES
REASON=GO_ED_SUITE8
```

## Décision (suite 7 -> suite 8)

La suite 7 a conclu qu'un patch minimal dans `tools/approval.py` serait
dangereux : retirer la règle hardline ferait tomber l'opération dans
`DANGEROUS_PATTERNS` (yolo, mode off, smart approval, cron approve,
allowlist persistante). Ajouter une exception basée sur des booléens fournis
par le modèle ne prouverait ni la provenance du GO ni l'état du disque.
`force=True` saute l'ensemble du moteur de guards.

**Option A retenue par Ed (GO suite 8)** : capacité destructrice one-shot
émise à la frontière utilisateur + outil dédié `governed_mkfs` + transport
QGA structuré + préchecks core fail-closed + audit. Le terminal générique
reste hardline.

## Ce qui a été implémenté (suite 8)

| Composant | Fichier | Rôle |
|---|---|---|
| Store de grants | `tools/destructive_grants.py` | issue/verify/consume/revoke/audit, fichier 0600, binding SHA256, nonce, TTL |
| Frontière utilisateur | `hermes_cli/subcommands/grant.py` | `hermes grant issue/list/revoke/audit` — CLI uniquement, jamais un tool modèle |
| Outil gouverné | `tools/governed_mkfs_tool.py` | `governed_mkfs` : verify -> prechecks -> TOCTOU -> exec structurée -> postcheck -> consume |
| Adaptateur QGA | `tools/qga_structured.py` | argv allowlisté, prechecks §13, postcheck blkid, parseur corrigé |
| Dispatch | `hermes_cli/main.py` | `cmd_grant` + parser |

## Red lines préservées

- `TERMINAL_POLICY_WEAKENED=NO` : `mkfs` reste hardline dans le terminal
  générique (prouvé en Track D : la commande pytest contenant "mkfs" a été
  bloquée par la hardline).
- `ARBITRARY_ROOT_SHELL_CAPABILITY=NO` : argv construit côté code de
  confiance, jamais de shell libre.
- `AGENT_SELF_AUTHORIZATION=NO` : aucun tool modèle ne peut émettre un grant.
- `CAPABILITY_REPLAY=NO` : consommation atomique, replay DENIED (prouvé en
  Track D réel).
- `ROOT_DEVICE_FORMAT=IMPOSSIBLE` : validation device (partition/loop
  uniquement, jamais disque entier, jamais root).

## Tests

- 32 tests policy (dont 3 nouveaux `TestPrecheckPartuuidSemantics`).
- 6 tests CLI grant.
- 1 test découverte registre.
- Track D réel : loop device 64M, exécution structurée PASS, replay DENIED.
- Baseline complète : en cours (voir REPORT.md).
