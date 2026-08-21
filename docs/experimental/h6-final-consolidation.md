# H6 Durable Workers final consolidation

Status: `H6_CODE_IN_PROGRESS`

Branch: `experimental/durable-workers-final-consolidation`

H5 backend baseline: `006527335a63f0c14014476c6d2d7d7788b82c6e`

Qualified H5 behavior baseline: `ea254053c82929fc44646b6cb4c8456498d5deb4`

Final API plugin candidate: `api-server-durable-workers 0.5.0`

Final tool plugin candidate: `durable-workers 0.2.0`

## Purpose

H6 is the final planned consolidation phase for the Durable Workers/Harness
workstream. It does not add a new worker capability, scheduler or operator
surface. Its purpose is to turn the H1-H5 experimental chain into a clean,
versioned and reviewable integration candidate.

The qualified H5 behavior remains authoritative for:

- durable worker identity and inbox;
- activation serialization and cold reactivation;
- authenticated read/write API and SSE invalidation;
- operator cancel, fail-closed recovery and CAS retry;
- task-DAG editing, READY/BLOCKED gating and real dispatch;
- task-aware cancel/recovery/crash handling;
- Harness operator controls and one-EventSource ownership.

## Formal storage schema

H6 introduces the first formal Durable Worker database schema version:

`PRAGMA user_version = 1`

Version 1 is deliberately the **already-qualified H5 layout**. H6 does not
rewrite existing durable rows or introduce a destructive migration.

The v1 contract includes:

- `durable_workers`;
- `durable_worker_messages`;
- `durable_worker_activations` including `owner_started_at`;
- `durable_worker_tasks`;
- `durable_worker_task_dependencies`;
- `durable_worker_task_runs`.

### Legacy adoption

H1-H5 databases have `user_version = 0`.

The H6 versioned store:

1. reads an existing version before mutation;
2. rejects a version newer than this build;
3. runs the qualified H1 additive base bootstrap;
4. ensures the H5 task-run audit table exists;
5. validates required columns and foreign-key integrity;
6. atomically stamps `user_version = 1`;
7. only then performs abandoned-activation recovery.

Existing durable data is preserved.

### Future-version downgrade protection

A database with `user_version > 1` is opened read-only for the version check
and rejected before H1 schema bootstrap can create or alter tables.

This prevents an older H6 binary from silently operating on a future storage
layout it does not understand.

### Explicit audit

`agent.durable_worker_schema.audit_schema()` performs a read-only audit:

- exact schema version;
- required table/column shape;
- `PRAGMA foreign_key_check`;
- `PRAGMA quick_check`.

The audit does not repair or rewrite data.

## Rollback contract

H6 v1 is intentionally H5-compatible.

Rolling the executable back to the qualified H5 code does **not** require a
database downgrade. H5 ignores `PRAGMA user_version` and can reopen the same
layout and durable rows.

No H6-only table or column is required for runtime behavior beyond the H5
layout already in use.

Operational rollback should still preserve a filesystem/SQLite backup before
any release change, but H6 does not require a reverse migration.

## Final store composition

`VersionedDurableWorkerStore` subclasses the qualified H1 store rather than
copying its state machine.

The final API adapter caches one versioned store wrapper per adapter. The store
object holds only the database path; individual operations continue to open
their own SQLite connections, so the cache avoids repeated schema bootstrap
without sharing a mutable SQLite connection between worker threads.

The opt-in tool plugin uses the same versioned store contract.

## API surface

`DurableWorkersFinalAPIServerAdapter` subclasses the qualified H5 task-recovery
adapter.

H6 adds:

- **0 routes**;
- **0 listeners**;
- **0 authentication mechanisms**;
- **0 browser-facing secrets**.

Default plugin behavior remains the stock `APIServerAdapter` unless
`platforms.api_server.extra.durable_workers_api` is explicitly enabled.

## Open-source readability

H6 treats reviewability as part of correctness.

The historical Durable Workers tool plugin has been reformatted from compact
one-line control flow into conventional Python while keeping the same action
surface. H6-specific schema and adapter code are kept in small modules with
explicit responsibilities and tests.

See [AI-assisted development](ai-assisted-development.md) for the voluntary
transparency note used by this contribution.

## H6 qualification boundary

Before H6 can become PASS, an isolated lab must prove at minimum:

- Python compilation and all H1-H6 targeted tests;
- legacy v0 database adoption to v1 with durable data preserved;
- explicit schema audit PASS;
- future schema rejection before listener startup and before mutation;
- H5 executable rollback compatibility against an H6-adopted database;
- final API route table is byte-for-contract equivalent to H5;
- tool plugin action surface is unchanged;
- one normal real Durable Worker activation after v0->v1 adoption;
- one real H5 task-DAG dispatch after adoption;
- restart/cold recovery remains correct;
- auth, isolation, SSE, cancel/retry and task recovery smoke remain green;
- principal runtime/config remain untouched.

A full replay of every H1-H5 scenario is unnecessary unless a smoke test shows
regression, but the final H6 lab should include a longer mixed-operation soak.

No PR, merge or principal runtime mutation is authorized by this document.
