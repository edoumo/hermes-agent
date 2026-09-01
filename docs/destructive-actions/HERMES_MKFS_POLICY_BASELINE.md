# HERMES mkfs policy baseline

Timestamp: `2026-09-01T20:00:33+02:00`

## Verdict

`BLOCKED_NEEDS_ED_DECISION`

La hardline est modifiable, mais Hermes ne possède aujourd'hui aucune représentation de confiance d'un GO destructif déjà présent dans un mandat. Une simple affirmation du modèle, une règle de configuration modifiable par l'agent ou `force=True` ne constituent pas une preuve d'autorisation utilisateur.

## Sources et runtime

- Dépôt de conception propre : `/home/edou/workspace/hermes-destructive-policy`
- Branche : `feat/destructive-explicit-approval`
- Base : `origin/main`
- HEAD : `894fc35337f3380897fe1a67d42aeb6403b359ef`
- Runtime gateway/WebUI partagé : `/usr/local/lib/hermes-agent`
- SHA runtime : `d5281f59819d2ea2ce6754faec2ce317c92366c8`
- Service gateway : actif/running au contrôle
- Service WebUI : actif/running au contrôle
- Le unit WebUI fixe `HERMES_WEBUI_AGENT_DIR=/usr/local/lib/hermes-agent` et `PYTHONPATH=/usr/local/lib/hermes-agent`.
- Le clone `/home/edou/src/hermes-agent` n'est pas la source active du WebUI au moment de cette baseline.

## Règle responsable

```text
policy_file=tools/approval.py
matcher=detect_hardline_command() -> _command_detection_variants() -> HARDLINE_PATTERNS_COMPILED
mkfs_rule=(_CMDPOS + r'mkfs(\.[a-z0-9]+)?\b', "format filesystem (mkfs)")
terminal_entry=tools/terminal_tool.py::terminal_tool()
```

## Précédence

Dans `check_all_command_guards()` :

1. skip uniquement pour certains backends isolés sans accès hôte ;
2. hardline ;
3. garde `sudo -S` sans secret configuré ;
4. `approvals.deny` utilisateur ;
5. yolo / `approvals.mode=off` ;
6. allowlist permanente ;
7. règles de contexte unattended/cron/single-query ;
8. Tirith + patterns DANGEROUS ;
9. prompt humain.

Dans `terminal_tool()`, `force=True` saute entièrement `check_all_command_guards()`. Il est donc impropre à une classe destructrice qui doit toujours refaire ses préchecks.

## Local / SSH / QGA

- Local et backend SSH passent par le même moteur d'approbation sur la chaîne de commande reçue.
- Il n'existe pas de politique QGA structurée ni de notion VMID/device/major-minor dans Hermes.
- Le canal actuel est un helper local qui encapsule SSH vers hote1 puis `qm guest exec 148`.
- Le runtime ancien bloque ce helper par correspondance lexicale large.
- L'amont récent ancre `mkfs` en position de commande et ne connaît pas ce helper comme shell carrier/transport. Une autorisation basée uniquement sur le texte brut ne peut donc ni prouver le tuple QGA ni garantir que le payload cité est la commande réellement exécutée.

Un appel direct et non exécutant de `detect_hardline_command()` sur l'amont a confirmé : commande nue `hardline=True`, mais payload imbriqué derrière `ssh ... qm guest exec ...` ou `qm guest exec ... bash -c ...` = `hardline=False` et `dangerous=False`. Ce test n'a lancé aucune commande système. Il prouve qu'un transport QGA structuré fait partie du correctif de sécurité, et pas seulement de l'UX.

## Baseline tests

```text
python3 -m pytest tests/tools/test_hardline_blocklist.py -q -o addopts=
246 passed

python3 -m pytest tests/tools/test_approval_deny_rules.py tests/tools/test_approval_windows.py -q -o addopts=
61 passed
```

Aucun fichier de production n'a été modifié avant cette baseline.
