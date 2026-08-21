# H4 Durable Worker Operational Recovery & Control

Status: `H4_OPERATIONAL_RECOVERY_STATUS=PASS`

Branch: `experimental/durable-workers-operational-recovery`

Qualified parent baseline: `bf336decb0ba298e85e5a34cf5b7a596f7dee2dc` (H2.1 PASS)

Qualified H4 behavior SHA: `2e14c4f719a9c85bb79b9a44dc72a15cecfb1c39`

Post-qualification test-maintenance SHA: `26547708c7f2ae2b3239a4b02875a7951a8ac8b7`

Qualified Harness H4 SHA: `decc07a86ef109f953e9f13433cd8990dff249ef`

Plugin version: `0.3.2`

## Objective

H4 adds explicit operator recovery and control without weakening H1/H2.1 durability guarantees. The API remains session scoped, uses the existing authenticated API-server listener, and never serializes live lifecycle objects.

The H4 backend slice contains three capabilities:

1. cancel a currently supervised activation;
2. retry the message associated with a terminal failed worker;
3. inspect session-scoped operational counts and the configured activation limit.

## Safety invariants

### Cancellation

Cancellation is not an optimistic state flip.

The durable transition is:

`STARTING|RUNNING -> CANCEL_REQUESTED`

while:

* worker remains `RUNNING`;
* parent message remains `PROCESSING`;
* the live child remains owned by the lifecycle service.

The API only attempts lifecycle cancellation if the current process has the capability-bearing live handle in `_dw_active_lifecycles`. A durable activation row alone is not sufficient proof of control.

If lifecycle cancellation is rejected, the durable marker is restored with a CAS to its previous `STARTING` or `RUNNING` state.

If cancellation is accepted, the background execution path continues to own terminal reconciliation.

Only after lifecycle reports terminal `CANCELLED` and the durable activation is still marked `CANCEL_REQUESTED` does H4 perform:

* activation -> `CANCELLED`;
* parent message -> `PENDING`;
* worker -> `DORMANT`.

That makes an explicitly cancelled turn retryable without erasing audit history.

Gateway drain remains fail closed because the drain path does not persist the H4 operator-cancel marker. A child cancelled by system drain therefore keeps the previous H2.1 behavior and leaves the worker `FAILED` for operator review/retry.

### Retry

Retry does not create or rewrite an activation row.

Preconditions:

* worker belongs to the addressed parent session;
* worker is `FAILED`;
* optional `expected_revision` matches;
* `last_activation_id` identifies a terminal non-success activation;
* that activation references a parent message in `FAILED` state.

Transition:

* message `FAILED -> PENDING`;
* worker `FAILED -> DORMANT`;
* worker revision increments;
* failed activation remains immutable evidence.

A later normal `/run` reservation creates a brand-new activation id.

### Operational summary

The summary exposes only session-scoped counts:

* worker states;
* active activation states;
* actionable parent-message states;
* configured maximum concurrent Durable Worker activations.

It deliberately does not expose global active usage, PIDs, process birth markers, live handles, or activity belonging to another session.

## H4 API additions

All routes inherit existing Hermes API bearer auth, CORS/profile scope, and the same listener.

### GET `/api/sessions/{session_id}/worker-operations`

Returns session-scoped worker/activation/message counts plus `configured_max_concurrent_activations`.

### POST `/api/sessions/{session_id}/workers/{worker_id}/retry`

Body:

```json
{
  "expected_revision": 7
}
```

`expected_revision` may be omitted or null. A stale value returns conflict.

### POST `/api/sessions/{session_id}/workers/{worker_id}/activations/{activation_id}/cancel`

Body:

```json
{
  "reason": "operator requested cancellation"
}
```

`reason` is optional, bounded to 500 safe characters.

Expected accepted response is HTTP 202 with durable status `CANCEL_REQUESTED`. This is an acknowledgement of cancellation intent and lifecycle acceptance, not a claim that the child is already terminal.

### Exact query contract and ownership ordering

H4 control routes reject non-empty query strings. The ordering is deliberately fail closed:

* `worker-operations`: session lookup -> query guard -> operational read;
* `retry`: session lookup -> read-only worker ownership projection -> query guard -> body -> mutation;
* `cancel`: session lookup -> read-only worker ownership projection -> query guard -> body -> activation/lifecycle mutation.

The worker ownership check uses the H2 projection opened with SQLite `mode=ro` and `query_only=ON`, so invalid query requests cannot construct the mutable H1 store or trigger abandoned-work recovery.

Owned object plus query returns HTTP 400 `invalid_durable_worker_request`. Foreign worker plus query preserves HTTP 404 `durable_worker_not_found`. Nonexistent session plus query preserves session-scoped 404.

## Implementation layout

`agent/durable_worker_control.py`

Owns the transactional H4 operator state transitions while reusing the H1 store as source of truth.

`agent/durable_worker_execution.py`

Adds terminal reconciliation for operator-marked `CANCELLED` activations.

`gateway/platforms/api_server_durable_control.py`

Extends the qualified H2.1 runtime adapter with the three H4 routes. It does not add a listener or auth implementation.

`plugins/platforms/api_server`

Version `0.3.2` selects the H4 control adapter only when `durable_workers_api` is explicitly enabled. Default behavior remains stock Hermes API server.

## Tests

H4 coverage includes:

* `tests/agent/test_durable_worker_control.py`;
* `tests/agent/test_durable_worker_execution_h4.py`;
* `tests/gateway/test_api_server_durable_workers_control.py`;
* `tests/gateway/test_api_server_durable_workers_control_queries.py`;
* `tests/plugins/test_api_server_durable_workers_platform.py`.

The B4-final real qualification exposed four stale unit-test mocks in `test_api_server_durable_workers_control.py`: the tests used `object.__new__` without mocking the newly required read-only ownership projection. This was test scaffolding only; real HTTP behavior passed. Commit `26547708c7f2ae2b3239a4b02875a7951a8ac8b7` updates those four mocks without modifying production behavior.

## Qualification result

`H4_OPERATIONAL_RECOVERY_STATUS=PASS`.

The isolated real-runtime qualification established:

* real operator cancellation through `CANCEL_REQUESTED` with worker/message lock preserved until terminal `CANCELLED`;
* no overlapping activation during cancellation;
* same-message rerun with a new activation id and subagent id, ending `SUCCEEDED`;
* system drain remains fail closed and produces a reviewable `FAILED` worker/message state;
* stale retry CAS is rejected and valid retry preserves historical failure evidence;
* post-retry run succeeds with a new activation and subagent;
* Harness Cancel, Retry and operational summary follow canonical API/SSE state without browser-side optimistic mutation;
* one EventSource per selected session, no reconnect storm, bounded DOM and no browser secret exposure;
* session isolation, CSRF and Bearer boundaries remain intact;
* B4 exact query rejection and ownership-before-query ordering are verified in real HTTP with zero durable mutation;
* principal runtime, legacy WebUI and principal configuration remained untouched; lab shutdown was clean.

Real qualification evidence is retained under the lab evidence sets for H4 initial, B4 and B4-final. The final B4 evidence archive SHA256 is `dbc81a6477694e8d80107dccde0fd1ef35acdb3d97c4ee290a385e39073e7c08`.

The behavior-qualified SHA remains `2e14c4f719a9c85bb79b9a44dc72a15cecfb1c39`. Later commit `26547708c7f2ae2b3239a4b02875a7951a8ac8b7` is test maintenance only and does not alter the qualified H4 runtime behavior.

No PR or merge is authorized at this stage.
