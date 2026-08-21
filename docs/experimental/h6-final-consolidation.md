# H6 Durable Workers final consolidation

Status: `H6_CLEAN_CANDIDATE_READY_FOR_QUALIFICATION`

Clean candidate branch: `experimental/durable-workers-h6-upstream-main`

Upstream base: `NousResearch/hermes-agent@fcbd1076a93841fa88855acce810e342a5b78101`

Qualified H5 behavior baseline: `ea254053c82929fc44646b6cb4c8456498d5deb4`

Final API plugin candidate: `api-server-durable-workers 0.5.0`

Final tool plugin candidate: `durable-workers 0.2.0`

## Purpose

H6 is the final planned consolidation phase for Durable Workers and the Harness integration. It does not add another scheduler, worker capability, API listener, or browser control. Its job is to turn the H1-H5 experimental chain into a clean, versioned, reviewable candidate on the current upstream codebase.

The H5 qualification remains authoritative for durable identity and inbox semantics, activation serialization, authenticated read/write API and SSE invalidation, operator cancel/retry/fail-closed recovery, task-DAG READY/BLOCKED dispatch, task-aware recovery, and Harness operation.

## Clean upstream integration

The final H6 candidate is rebuilt directly on the current upstream commit instead of carrying the historical experimental branch ancestry. The contribution is additive: existing upstream production files are not overwritten.

The candidate is intentionally split into reviewable commits for runtime/documentation and consolidated H1-H6 tests. Historical phase reports are not required in the public diff; the qualification ledger preserves the relevant lineage.

## Formal storage schema

H6 introduces the first formal Durable Worker database schema version:

`PRAGMA user_version = 1`

Version 1 is deliberately the already-qualified H5 layout. H6 does not rewrite existing durable rows or require a destructive migration.

The v1 contract includes:

- `durable_workers`;
- `durable_worker_messages`;
- `durable_worker_activations` including `owner_started_at`;
- `durable_worker_tasks`;
- `durable_worker_task_dependencies`;
- `durable_worker_task_runs`.

### Legacy adoption

H1-H5 databases have `user_version = 0`. The H6 versioned store reads the existing version before mutation, rejects a version newer than this build, performs the additive base bootstrap, ensures the H5 task-run audit table exists, validates required columns and foreign-key integrity, atomically stamps `user_version = 1`, and only then performs abandoned-activation recovery.

Existing durable data is preserved.

### Future-version downgrade protection

A database with `user_version > 1` is inspected without running the H1 bootstrap and is rejected before schema mutation or listener startup. An older H6 binary therefore cannot silently operate on a future storage layout it does not understand.

### Explicit audit

`agent.durable_worker_schema.audit_schema()` performs a read-only audit of the exact schema version, required table/column shape, `PRAGMA foreign_key_check`, and `PRAGMA quick_check`. The audit does not repair or rewrite data.

## Rollback contract

H6 v1 is intentionally H5-compatible. Rolling the executable back to the qualified H5 code does not require a database downgrade: H5 ignores `PRAGMA user_version` and can reopen the same table/column layout. Operational rollback should still preserve a SQLite/filesystem backup before a release change.

## Final store composition

`VersionedDurableWorkerStore` subclasses the qualified H1 store instead of copying its state machine. The final API adapter caches one versioned store wrapper per adapter; the wrapper holds only the database path, while individual operations continue to open their own SQLite connections. The opt-in tool plugin uses the same versioned store contract.

## API surface

`DurableWorkersFinalAPIServerAdapter` subclasses the H5 task-recovery adapter. H6 adds **0 routes**, **0 listeners**, **0 authentication mechanisms**, and **0 browser-facing secrets**. Default plugin behavior remains the stock `APIServerAdapter` unless `platforms.api_server.extra.durable_workers_api` is explicitly enabled.

## Open-source readability and AI disclosure

Reviewability is part of H6 correctness. The Durable Workers tool plugin has conventional Python formatting, H6 schema/adapter responsibilities live in small explicit modules, and the final architecture is documented separately.

See [AI-assisted development](ai-assisted-development.md) for the scoped `🤖 AI-assisted development` disclosure. It applies to this Durable Workers contribution, not unrelated upstream Hermes Agent code.

## Qualification boundary

The clean candidate is ready for isolated H6 qualification, not yet declared PASS. The final lab must prove at minimum:

- Python compilation and the consolidated H1-H6 tests on the clean upstream-based branch;
- non-destructive adoption of a populated H5 v0 database to v1;
- schema audit PASS and future-version rejection before mutation/listener startup;
- H5 rollback compatibility against an H6-adopted database;
- default stock adapter and opt-in final adapter behavior with no extra listener/route drift;
- real Durable Worker and task-DAG execution on the current upstream base;
- cancel/retry/recovery/crash/SSE/isolation/security smokes;
- Harness integration against its own current upstream-based clean candidate;
- no principal runtime or configuration mutation.

A complete replay of every historical H1-H5 scenario is unnecessary unless a smoke test reveals regression.

No PR, merge, main/master update, or principal runtime mutation is authorized by this document.
