# H2 ADR — Durable Workers API for Hermes Harness UI

## Status

`H2_API_FOUNDATION_IN_PROGRESS`

H1 real-runtime prerequisite: `H1_REAL_RUNTIME_RECIPE_PASS`.

## Decision

Durable Workers will be exposed through the existing Hermes API server rather than through a new standalone service.

The browser-facing hierarchy will be session-scoped:

```text
/api/sessions/{session_id}/workers
/api/sessions/{session_id}/workers/{worker_id}
/api/sessions/{session_id}/workers/{worker_id}/messages
/api/sessions/{session_id}/workers/{worker_id}/activations
/api/sessions/{session_id}/worker-tasks
```

Write/run routes will be added only after the read contract is qualified.

## Why session-scoped

H1 real-runtime validation proved that durable workers require a stable parent identity across process boundaries. A named Hermes session supplied that stable identity during the cold-resume recipe.

Using the existing persisted Hermes session id in the HTTP route therefore makes the durable parent relationship explicit instead of relying on an incidental live-process identity.

A future workspace abstraction can supersede this route key without changing worker identity semantics.

## Existing Hermes server reused

Hermes already exposes authenticated `/api/sessions` and `/v1/runs` surfaces through `gateway/platforms/api_server.py`.

H2 will reuse:

* the existing API listener;
* `API_SERVER_KEY` Bearer authentication;
* profile URL scoping;
* configured CORS policy;
* existing session identifiers;
* existing SSE conventions where practical.

H2 must not create an unauthenticated second HTTP listener.

## Read model first

The first H2 implementation is `agent.durable_workers_api.DurableWorkersProjection`.

It is intentionally read-only and opens `durable-workers.db` using SQLite read-only/query-only mode.

The UI never reads SQLite directly.

The projection provides bounded keyset pagination for:

* workers;
* messages;
* activations;
* tasks.

Default page size is 50 and the hard maximum is 100.

Pagination cursors are feed-specific opaque transport tokens. They are not authorization capabilities. Every SQL query applies `parent_session_id` scope before cursor filtering.

## Privacy boundary

Activation recovery metadata such as `owner_pid` and `owner_started_at` is host-internal and is omitted from the public projection.

Worker and task records remain scoped to the owning Hermes session.

Cross-session lookup returns not-found rather than leaking the existence of another session's worker.

## H1 constraints preserved

### Toolsets

A worker can only request toolsets already enabled for its parent. The API must fail closed if a request attempts to broaden the parent's capabilities.

### Timeout

Hermes currently rejects per-launch `SubagentLaunchRequest.timeout_seconds`.

If an H2 endpoint accepts a deadline later, it must mean host-side wait/cancel behavior, not a delegated child launch timeout.

### Activation identity

The API must preserve:

`worker_id != activation_id != subagent_id`

A new activation after cold resume receives a new activation and subagent identity while retaining the worker identity.

## Proposed read endpoints

### List workers

```http
GET /api/sessions/{session_id}/workers?limit=50&cursor=...
```

Response:

```json
{
  "items": [],
  "next_cursor": null,
  "has_more": false
}
```

### Worker detail

```http
GET /api/sessions/{session_id}/workers/{worker_id}
```

### Worker messages

```http
GET /api/sessions/{session_id}/workers/{worker_id}/messages?limit=50&cursor=...
```

### Worker activations

```http
GET /api/sessions/{session_id}/workers/{worker_id}/activations?limit=50&cursor=...
```

### Task board

```http
GET /api/sessions/{session_id}/worker-tasks?limit=50&cursor=...
```

## Proposed write endpoints, not yet implemented

```text
POST /api/sessions/{session_id}/workers
POST /api/sessions/{session_id}/workers/{worker_id}/messages
POST /api/sessions/{session_id}/workers/{worker_id}/run
POST /api/sessions/{session_id}/worker-tasks
PATCH /api/sessions/{session_id}/worker-tasks/{task_id}
```

The run route is the sensitive seam because Hermes' public subagent lifecycle requires a live host-owned parent agent. It will be integrated only after the API server's session-agent construction path is explicitly reused, not duplicated.

## Event delivery direction

The target for Hermes Harness UI is incremental updates, but H2 will not make SQLite a browser event source.

Preferred progression:

1. bounded snapshot endpoints;
2. host-owned durable event ledger or normalized lifecycle events;
3. SSE endpoint using Hermes' existing streaming conventions;
4. reconnect cursor for missed events.

The event API must never require loading an entire worker transcript to determine what changed.

## Memory and large-session objective

Pagination is a first-class requirement because Hermes Harness UI is intended to avoid the large-session memory failure modes observed in the historical WebUI.

No endpoint should return an unbounded transcript, activation journal or task history.

## Testing checkpoint

Initial isolated projection tests: `5 passed`.

Covered:

* bounded worker pagination;
* parent scoping;
* foreign worker fail-closed behavior;
* activation process-metadata redaction;
* task dependency projection;
* cursor type isolation;
* invalid limit rejection.

## Next implementation step

Integrate read-only handlers into the existing Hermes API server with the existing authentication/session lookup conventions, then test them through aiohttp without enabling any new public listener.
