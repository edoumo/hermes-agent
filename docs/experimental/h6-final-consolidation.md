# H6 Durable Workers final consolidation

Status: `H6_FINAL_CONSOLIDATION_STATUS=PASS`

Integration readiness: `HERMES_DURABLE_WORKERS_INTEGRATION_READINESS=PASS`

Clean candidate branch: `experimental/durable-workers-h6-upstream-main`

Upstream base: `NousResearch/hermes-agent@fcbd1076a93841fa88855acce810e342a5b78101`

Behavior-qualified H6 SHA: `011fe3c2fb20385c97ecad450ded02d0982ae3db`

Final test-hygiene HEAD: `07dd7bd93411f3486e405b55dda48892395ea637`

Paired Harness H6 SHA: `9a6ea47e969489149c3964c6de8bdb9923acd3cc`

Final API plugin: `api-server-durable-workers 0.5.0`

Final tool plugin: `durable-workers 0.2.0`

## Purpose

H6 is the final planned consolidation phase for Durable Workers and the Harness integration. It adds no new scheduler, worker capability, API listener, or browser control. Its job is to turn the H1-H5 experimental chain into a clean, versioned, reviewable candidate on the recorded upstream codebase.

The H5 qualification remains authoritative for durable identity and inbox semantics, activation serialization, authenticated read/write API and SSE invalidation, operator cancel/retry/fail-closed recovery, task-DAG READY/BLOCKED dispatch, task-aware recovery, and Harness operation. H6 proves that those contracts survive the clean current-upstream transplant and the formalized storage layer.

## Clean upstream integration

The H6 candidate was rebuilt directly on the recorded upstream commit instead of carrying historical experimental branch ancestry. The contribution is additive: existing upstream production files are not overwritten.

At final qualification the backend candidate was zero commits behind its recorded upstream base. The only commit after the behavior-qualified SHA changed `tests/plugins/test_durable_workers_tool_plugin_h6.py`; no `agent/`, `gateway/`, or `plugins/` production file changed after real-runtime qualification.

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

Final H6 qualification proved adoption on a populated copy of an H5 lab database with IDs and row counts preserved. The only row-content changes were the expected crash-recovery reconciliation of an orphaned activation.

### Future-version downgrade protection

A database with `user_version > 1` is inspected without running the H1 bootstrap and is rejected before schema mutation or listener startup. Final H6 qualification proved that a forced v2 database remained byte-for-contract unchanged after rejection and no listener started.

### Explicit audit

`agent.durable_worker_schema.audit_schema()` performs a read-only audit of the exact schema version, required table/column shape, `PRAGMA foreign_key_check`, and `PRAGMA quick_check`. Final qualification returned an empty foreign-key check and `quick_check = ok`.

## Rollback contract

H6 v1 is intentionally H5-compatible. Final qualification reopened the H6-adopted v1 database with the behavior-qualified H5 code without downgrading `user_version`, preserved workers/messages/activations/tasks/dependencies/task-runs, and successfully performed a safe H5 operation.

## Final store composition

`VersionedDurableWorkerStore` subclasses the qualified H1 store instead of copying its state machine. The final API adapter caches one versioned store wrapper per adapter; the wrapper holds only the database path, while individual operations continue to open their own SQLite connections. The opt-in tool plugin uses the same versioned store contract.

## API surface

`DurableWorkersFinalAPIServerAdapter` subclasses the H5 task-recovery adapter. H6 adds **0 routes**, **0 listeners**, **0 authentication mechanisms**, and **0 browser-facing secrets**. Default plugin behavior remains the stock `APIServerAdapter` unless `platforms.api_server.extra.durable_workers_api` is explicitly enabled.

## Final qualification

The final H6 campaign proved:

- backend `py_compile` PASS;
- consolidated backend suite `91/91 PASS` after test-hygiene closure;
- Harness suite `36/36 PASS` and JavaScript syntax checks PASS;
- fresh schema v1, H5 v0 adoption, failed-adoption atomicity, future-version fail-closed behavior, and H5 rollback PASS;
- real DeepSeek Durable Worker and task-DAG execution PASS;
- cancel lock/terminal transition/redispatch PASS;
- fail-closed task recovery PASS;
- crash/restart ABANDONED reconciliation and redispatch PASS;
- session isolation, auth, CSRF, browser secret boundary, SSE ownership and bounded DOM PASS;
- shared capacity and mixed-operation soak PASS;
- principal gateway, WebUI, dashboard and configuration untouched.

The real-runtime evidence archive is recorded as:

`h6-final-consolidation-evidence.tar.gz`

SHA-256:

`e7ac0b0d89e13055b94046d7aefed9123e1b40a9454bdd8a56300120a7755d91`

The subsequent test-hygiene pass changed only the static plugin contract test, replacing a substring false positive with AST-based call inspection.

## Open-source readability and AI disclosure

Reviewability is part of H6 correctness. The Durable Workers tool plugin has conventional Python formatting, H6 schema/adapter responsibilities live in small explicit modules, and the final architecture is documented separately.

See [AI-assisted development](ai-assisted-development.md) for the scoped `🤖 AI-assisted development` disclosure. It applies to this Durable Workers contribution, not unrelated upstream Hermes Agent code.

## Release gate

Technical integration readiness is PASS, but no upstream pull request is authorized by this document. Before PR preparation, the maintainer should perform a hands-on user acceptance pass of the Harness and Durable Workers workflow and report any product or ergonomics issues.

No PR, merge, main/master update, or principal runtime mutation is authorized without explicit maintainer approval.
