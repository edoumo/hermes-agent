# Hermes runtime qualification

## Verdict

`NOT_DEPLOYED_ARCHITECTURE_DECISION_REQUIRED`

## Runtime observé

```text
runtime_path=/usr/local/lib/hermes-agent
runtime_sha=d5281f59819d2ea2ce6754faec2ce317c92366c8
runtime_describe=v2026.7.20-8160-gd5281f5981
webui_agent_dir=/usr/local/lib/hermes-agent
gateway=active/running
webui=active/running
```

Le runtime contient des fichiers non suivis préexistants. Ils n'ont pas été modifiés.

## Déploiement

```text
files_copied=NONE
pyc_deleted=NONE
services_restarted=NONE
config_changed=NONE
rollback_executed=NO
```

## Rollback prévu après futur GO

Avant tout déploiement futur : snapshot/backup exact des fichiers modifiés, SHA et checksums ; compilation avec le Python runtime ; tests ciblés ; copie atomique ; suppression ciblée des seuls `.pyc` concernés ; restart contrôlé du composant réellement importeur ; healthcheck gateway et HTTPS WebUI ; restauration immédiate si régression.

Aucun rollback n'est nécessaire pour cette session, car aucune mutation runtime n'a eu lieu.
