# H5 Durable Task Orchestration

Status: `H5_BACKEND_CODE_IN_PROGRESS`

Branch: `experimental/durable-workers-task-orchestration`

H4 baseline: `ab693e298d76a9fd8d65f174870a7c88cf68c962`

Plugin candidate: `api-server-durable-workers 0.4.0`

## Objective

H5 turns the existing Durable Worker task metadata into an operational DAG without introducing a second execution engine.

H1/H4 remain authoritative for:

- durable worker identity;
- durable inbox/messages;
- activation serialization;
- subagent lifecycle;
- cancellation and retry;
- crash recovery;
- authenticated API/SSE.

H5 owns only:

- safe editing of pending task definitions and worker assignment;
- CAS-safe dependency add/remove;
- a bounded read-only graph projection;
- atomic dispatch of a READY task into the existing Durable Worker activation lifecycle;
- projection of terminal activation results back into task state.

## Task state semantics

Existing task states remain:

- `pending`
- `in_progress`
- `completed`
- `failed`
- `cancelled`

`ready=true` is derived only when a task is `pending` and every blocker is `completed`.

H5 never persists a separate READY state.

## DAG editing

Task definition, worker assignment and dependency changes are allowed only while a task is `pending`.

Every H5 edit uses `expected_revision`.

Dependency additions reject cycles. Dependency add/remove increments the task revision when the edge changes.

The legacy H2.1 task routes remain available for compatibility; H5 UI uses the stricter H5 routes.

## Atomic task dispatch

A H5 dispatch is not a client-side sequence of `enqueue` then `run`.

`DurableTaskOrchestrator.reserve_ready_task()` uses one `BEGIN IMMEDIATE` transaction to validate and reserve all durable state.

Preconditions:

- task belongs to the addressed session;
- task is `pending`;
- `expected_revision` matches;
- all dependencies are completed;
- a worker is assigned and belongs to the same session;
- worker is `DORMANT`;
- worker has no older pending parent inbox message.

Atomic transition:

- create task-correlated parent message in `PROCESSING`;
- create Durable Worker activation in `STARTING`;
- worker `DORMANT -> RUNNING`, revision increments;
- task `pending -> in_progress`, revision increments;
- persist task/message/activation audit link.

The returned reservation is executed by the already-qualified H2.1/H4 activation path.

No live `AIAgent`, lifecycle handle, callback, thread or process object is persisted.

## Result reconciliation

When the existing lifecycle finishes:

- `SUCCEEDED` -> task `completed`;
- operator `CANCELLED` with H4 `retryable=true` -> task back to `pending`;
- fail-closed/system failure -> task `failed`;
- `CANCEL_REQUESTED` remains `in_progress` until durable terminal/recovery state is known.

The worker/message/activation transitions remain owned by H1/H4. H5 only updates the task and its audit row after observing that result.

## Audit table

H5 adds:

`durable_worker_task_runs`

It records only durable identifiers and result metadata:

- task id;
- worker id;
- message id;
- activation id;
- state;
- timestamps;
- bounded summary/error.

It does not store:

- PID/birth marker;
- live lifecycle handles;
- credentials;
- model secrets;
- browser state.

## Read-only graph projection

`DurableTaskGraphProjection` opens SQLite with:

- `mode=ro`;
- `PRAGMA query_only=ON`.

The graph is bounded to 100 task nodes and returns:

- tasks;
- `blocked_by`;
- `dependents`;
- derived `ready`;
- edges;
- state counts;
- latest durable task-run identifiers/metadata when available;
- `truncated=true` when the session has more nodes than returned.

It remains session scoped and excludes host recovery details.

## H5 API additions

### GET `/api/sessions/{session_id}/worker-task-graph`

Optional `limit`, 1..100.

### POST `/api/sessions/{session_id}/worker-tasks/{task_id}/edit`

Supports pending-task changes to:

- `subject`;
- `description`;
- `worker_id` (including null/unassigned);
- required `expected_revision`.

### POST `/api/sessions/{session_id}/worker-tasks/{task_id}/dependencies/add`

Body includes:

- `blocked_by_task_id`;
- `expected_revision`.

### POST `/api/sessions/{session_id}/worker-tasks/{task_id}/dependencies/remove`

Same CAS contract.

### POST `/api/sessions/{session_id}/worker-tasks/{task_id}/dispatch`

Body:

```json
{"expected_revision": 7}
```

Accepted dispatch returns HTTP 202 with task, worker, message and activation ids.

## API inheritance

`DurableWorkersTaskOrchestrationAPIServerAdapter` subclasses the qualified H4 `DurableWorkersControlAPIServerAdapter`.

No listener is added.

Default `durable_workers_api` off behavior remains stock `APIServerAdapter`.

H5 dispatch shares the existing process-wide activation capacity and `_dw_dispatch_lock` used by normal Durable Worker runs.

## Tests added

- `tests/agent/test_durable_task_orchestration.py`
- `tests/gateway/test_api_server_durable_task_orchestration.py`
- H5 factory expectation in `tests/plugins/test_api_server_durable_workers_platform.py`

## Qualification boundary

The authoring environment cannot resolve `github.com`, so no repository-level pytest result is claimed here.

Before H5 can become PASS, an isolated lab must prove:

- repository tests green;
- graph/session isolation;
- CAS edit/dependency behavior;
- cycle rejection;
- real READY task dispatch with DeepSeek;
- automatic task `in_progress -> completed` on success;
- operator cancel returns task to `pending` only after terminal H4 cancellation;
- system drain/failure yields task `failed`;
- retry/reset and redispatch create a new activation;
- no task dispatch overtakes an older pending worker inbox message;
- H4 cancel/retry and H3 SSE remain non-regressed;
- principal runtime remains untouched.

No PR, merge or principal runtime mutation is authorized.
