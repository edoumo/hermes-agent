# H4 Durable Worker Operational Recovery & Control

Status: `H4_BACKEND_CODE_IN_PROGRESS`

Branch: `experimental/durable-workers-operational-recovery`

Qualified parent baseline: `bf336decb0ba298e85e5a34cf5b7a596f7dee2dc` (H2.1 PASS)

## Objective

H4 adds explicit operator recovery and control without weakening H1/H2.1 durability guarantees. The API remains session scoped, uses the existing authenticated API-server listener, and never serializes live lifecycle objects.

The first H4 backend slice contains three capabilities:

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

## Implementation layout

`agent/durable_worker_control.py`

Owns the transactional H4 operator state transitions while reusing the H1 store as source of truth.

`agent/durable_worker_execution.py`

Adds terminal reconciliation for operator-marked `CANCELLED` activations.

`gateway/platforms/api_server_durable_control.py`

Extends the qualified H2.1 runtime adapter with the three H4 routes. It does not add a listener or auth implementation.

`plugins/platforms/api_server`

Version `0.3.0` selects the H4 control adapter only when `durable_workers_api` is explicitly enabled. Default behavior remains stock Hermes API server.

## Tests added

* `tests/agent/test_durable_worker_control.py`
* `tests/agent/test_durable_worker_execution_h4.py`
* `tests/gateway/test_api_server_durable_workers_control.py`

The existing platform factory test is updated to expect the H4 adapter while preserving the default-off contract.

## Qualification status

No main runtime has been changed and no real H4 recipe has run yet.

The development environment cannot currently resolve `github.com`, so repository pytest execution must not be claimed from the authoring environment. Integrated tests and real lifecycle cancellation will be a later isolated lab gate after code/UI completion.

No PR or merge is authorized at this stage.
