# H2 Durable Workers API Final Report

## Qualification verdict

`H2_API_QUALIFICATION_STATUS=PASS`

Qualified commit:

`453693eacae16a21ec73d4e67c5225ae7bc9a012`

Qualified on `srv-hermes` in the isolated laboratory:

`/home/edou/lab/hermes-durable-workers-h2`

The production Hermes runtime and its main configuration were not modified.

## Evidence summary

The H2 read-only API surface passed real-environment qualification with:

* 11/11 H2 targeted tests passing;
* 12/12 H1 non-regression tests passing, including 8 Durable Worker tests and 4 public subagent lifecycle tests;
* default factory behavior returning the stock `APIServerAdapter` when the H2 opt-in is absent or false;
* explicit `durable_workers_api: true` selecting `DurableWorkersAPIServerAdapter`;
* no second HTTP listener;
* inherited Hermes bearer authentication, CORS, profile multiplexing and session handling;
* bounded cursor pagination;
* fail-closed cross-session isolation;
* process-owner metadata excluded from public activation responses;
* empty Durable Worker store handled without side effects;
* native Hermes API routes continuing to work with H2 enabled.

## Qualified routes

The qualified read-only contract contains exactly these five Durable Worker routes:

* `GET /api/sessions/{session_id}/workers`
* `GET /api/sessions/{session_id}/workers/{worker_id}`
* `GET /api/sessions/{session_id}/workers/{worker_id}/messages`
* `GET /api/sessions/{session_id}/workers/{worker_id}/activations`
* `GET /api/sessions/{session_id}/worker-tasks`

No Durable Worker write route was part of the qualified H2 baseline.

## Real HTTP recipe

A temporary H2 API server was bound only to `127.0.0.1` on an isolated port.

The recipe demonstrated:

* missing bearer token => HTTP 401;
* invalid bearer token => HTTP 401;
* valid laboratory bearer token => success;
* missing session => HTTP 404 without implicit session creation;
* cross-session worker reads => HTTP 404 without existence leakage;
* worker pagination limits from 1 through 100 with opaque feed-scoped cursors;
* `owner_pid` and `owner_started_at` present in SQLite but absent from API output;
* no Durable Worker database => empty collection responses and worker detail 404;
* native `/health`, `/v1/models`, `/api/sessions` and `/api/sessions/{session_id}` behavior preserved.

## Infrastructure integrity

`MAIN_RUNTIME_TOUCHED=NO`

`MAIN_CONFIG_TOUCHED=NO`

The temporary H2 server was stopped and its laboratory port released after qualification.

## Evidence archive

Evidence directory:

`/home/edou/lab/hermes-durable-workers-h2/evidence-h2/`

Archive:

`h2-api-qualification-evidence.tar.gz`

SHA256:

`0fdbc99da8a29e206ce59a1487b46d6a7f9cf86a13e2b78b0223a74b6690f650`

## Non-blocking environment note

SQLite 3.50.4 emitted the pre-existing WAL-reset warning in the laboratory. It was unrelated to H2 behavior and is outside this milestone.

## H2.1 boundary

All commits after the qualified SHA belong to H2.1 and require their own qualification before they can inherit the H2 PASS verdict.

H2.1 is intended to add the authenticated write/control plane and a bounded event stream required by Hermes Harness UI while preserving the qualified H2 architecture: one Hermes API listener, stock behavior by default, explicit opt-in, session-scoped authority, and SQLite remaining an internal implementation detail.