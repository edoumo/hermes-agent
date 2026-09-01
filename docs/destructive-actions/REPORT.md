# REPORT — CI/CD suite7 — Hermes destructive storage policy

## Verdict décisionnel

`BLOCKED_NEEDS_ED_DECISION`

## Cause exacte

Hermes sait demander une approbation humaine pour une commande dangereuse, mais ne sait pas représenter de manière de confiance un GO destructif déjà inclus dans un mandat. Un attachment non signé n'est pas à lui seul une preuve d'adoption de toutes ses instructions ; `user_task` est enrichi par le gateway et ne peut pas faire autorité. Le moteur terminal ne reçoit pas de reçu utilisateur lié au tuple ; `force=True` saute les guards ; le helper QGA est un transport shell opaque. Un probe direct du détecteur amont confirme en plus que le payload imbriqué `ssh/qm guest exec` n'est actuellement classé ni hardline ni dangereux. Une exception locale dans la regex ne pourrait donc garantir ni provenance du GO, ni binding VM/device, ni préchecks fail-closed, ni TOCTOU.

Le §29 impose l'arrêt dans ce cas. Le §27 exige un GO séparé pour le changement architectural majeur nécessaire.

## Réalisé

- hardline, matcher et précédence local/SSH/QGA localisés ;
- runtime et worktree sources identifiés ;
- 12 hardlines inventoriées ;
- autres commandes stockage recherchées sans élargissement ;
- 307 tests baseline PASS ;
- doctrine et architecture sûre proposées ;
- huit livrables créés ;
- aucune mutation runtime, aucun restart, aucune opération VM148.

## Non réalisé par sécurité

- aucun test RED d'une API non décidée ;
- aucun patch ;
- aucun déploiement ;
- aucun test loop destructif ;
- aucun formatage VM148 ;
- aucune reprise Mailcow.

## Décision requise

Autoriser ou refuser l'Option A : une capacité destructrice one-shot créée à la frontière utilisateur, un outil dédié `governed_mkfs` laissant le terminal générique en hardline, un transport QGA structuré, des préchecks core fail-closed et un audit atomique. C'est la plus petite architecture qui satisfait réellement le mandat, mais elle traverse gateway/WebUI/agent/exécution/audit et constitue donc le GO séparé explicitement prévu.

## État final

```text
status=BLOCKED_NEEDS_ED_DECISION
blocker=NO_TRUSTED_REPRESENTATION_OF_EXISTING_EXPLICIT_GO
architecture_change_required=YES
architecture_change_class=MAJOR
recommended_option=TRUSTED_ONE_SHOT_CAPABILITY_PLUS_GOVERNED_MKFS_PLUS_STRUCTURED_QGA
policy_weakened=NO
runtime_mutated=NO
services_restarted=NO
vm148_mutated=NO
mailcow_state_replayed=NO
baseline_tests=307_PASS
```
