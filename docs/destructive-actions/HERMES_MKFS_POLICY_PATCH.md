# Hermes mkfs policy patch decision

## Patch status

```text
PATCH_STATUS=NOT_APPLIED
CODE_CHANGE=NONE
RUNTIME_CHANGE=NONE
REASON=MANDATE_STOP_CONDITION_TRIGGERED
```

## Pourquoi un patch minimal dans `tools/approval.py` serait dangereux

Retirer simplement la règle hardline ferait tomber l'opération dans `DANGEROUS_PATTERNS`, où elle pourrait être autorisée par yolo, mode off, smart approval, cron approve ou allowlist persistante. Cela violerait explicitement les red lines.

Ajouter une exception basée sur des booléens fournis dans les arguments du tool ferait confiance au modèle pour affirmer `explicit_user_go=true` et `prechecks=PASS`. Cela ne prouve ni la provenance du GO ni l'état du disque.

Réutiliser `force=True` est également interdit : `terminal_tool()` saute alors l'ensemble du moteur de guards.

## Surface minimale réellement nécessaire

Une correction conforme toucherait au minimum :

- l'ingress CLI/gateway/WebUI pour émettre un reçu utilisateur de confiance ;
- le stockage/session pour conserver et consommer atomiquement ce reçu ;
- `agent/tool_executor.py` / dispatch pour transporter un identifiant opaque ;
- `tools/approval.py` pour la nouvelle classe et sa précédence ;
- `tools/terminal_tool.py` pour exécuter les préchecks via le backend réel sans `force` ;
- un adaptateur QGA structuré pour lier node/VMID/commande guest ;
- le système d'audit pour prechecks/décision/exécution/postchecks ;
- les tests CLI, gateway, terminal local/SSH/QGA et les docs.

Ce changement traverse plusieurs trust boundaries et constitue un `changement architectural majeur Hermes` au sens du §27. Il exige donc un GO séparé.

## Options soumises à décision

### Option A — recommandée

Capacité destructrice one-shot émise par l'interface utilisateur + outil dédié `governed_mkfs` + transport QGA structuré + préchecks core + audit. Le terminal générique reste hardline. Répond à tous les critères mais représente une évolution architecturale.

### Option B — non conforme au mandat actuel

Prompt humain one-shot juste avant l'action via l'approval UI existante. Plus petit, mais redemande une confirmation et ne représente pas le GO déjà présent ; les préchecks et QGA restent à intégrer. Ne satisfait pas le §13.

Aucune branche distante ni PR n'a été créée, car il n'existe pas encore de choix d'architecture autorisé à proposer comme code.
