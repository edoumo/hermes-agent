# Hermes mkfs policy tests

## Baseline exécutée

```text
hardline_blocklist=246 PASS
approval_deny_rules_plus_windows=61 PASS
TOTAL_BASELINE=307 PASS
qga_nested_payload_detector_probe=BARE_BLOCKED__NESTED_QGA_NOT_CLASSIFIED
```

Le probe QGA a appelé le détecteur Python directement ; aucune commande n'a été exécutée et aucun block device n'a été touché.

## Tests RED non écrits

Le mandat impose TDD, mais écrire l'API de test avant de choisir la frontière de confiance figerait une architecture non autorisée. La stop-condition est évaluée avant Track C.

Après GO architectural pour l'Option A, les premiers tests RED devront couvrir :

1. refus sans reçu de capacité de confiance ;
2. refus d'un booléen `explicit_user_go=true` seulement fourni par le modèle ;
3. refus d'un reçu expiré, consommé ou appartenant à une autre session/utilisateur ;
4. refus d'un tuple VM/device/fs/label différent ;
5. refus root, mounted, swap, PV, mdraid, crypt active, holders ;
6. refus filesystem ou signature existante ;
7. refus si un precheck vaut unknown/ambiguous ;
8. refus si major:minor/taille/parent/mount changent entre contrôle et action ;
9. refus sous yolo/mode off/smart/cron/allowlist/force ;
10. autorisation one-shot d'une loop device vide et exacte ;
11. seconde utilisation du même reçu refusée ;
12. audit complet PASS et secrets absents ;
13. transport QGA : le VMID et le payload guest réellement exécuté correspondent au reçu ;
14. non-régression de toutes les autres hardlines.

## Destruction contrôlée

Aucun loop device n'a été créé et aucun formatage éphémère n'a été exécuté, car il n'existe pas encore de policy capable d'autoriser cette opération selon les invariants ci-dessus.
