# Hermes Durable Workers architecture

Status: experimental integration candidate

## Overview

Hermes Durable Workers add persistent worker identity and durable work queues to
Hermes without introducing a second agent runtime. A durable worker is a
persistent identity, inbox, transcript and activation history. Each unit of
work still executes as a fresh Hermes subagent through the existing
`SubagentLifecycleService`.

The central invariant is:

> **durable identity, ephemeral execution**

No live `AIAgent`, process, thread, socket, callback or lifecycle handle is
serialized to SQLite.

The implementation is opt-in. Stock Hermes behavior remains unchanged unless
the Durable Workers plugin/API extension is explicitly enabled.

## Architecture

```text
Hermes parent session
        |
        +-- Durable Worker identity (SQLite)
        |      |
        |      +-- durable inbox / transcript
        |      +-- activation history
        |      +-- optional task assignment
        |
        +-- reservation transaction
               |
               +-- fresh Hermes subagent activation
                       |
                       +-- SubagentLifecycleService
                       +-- existing model/tool/runtime routing
```

The authenticated API surface and Harness UI are consumers of the same durable
state. They do not own the worker lifecycle.

## Persistent entities

### Worker

A worker stores:

- `worker_id`;
- parent session ownership;
- label and role;
- optional model/toolset preferences;
- state and revision;
- latest activation id.

Worker states are:

- `DORMANT` — available for a new reservation;
- `RUNNING` — one activation owns the worker mutex;
- `FAILED` — operator review/recovery required;
- `DISABLED` — intentionally unavailable.

### Message

Parent messages form the durable inbox. Worker result messages form the durable
transcript.

Relevant parent-message states are:

- `PENDING`;
- `PROCESSING`;
- `CONSUMED`;
- `FAILED`.

Worker reports are stored as `COMPLETE` messages.

### Activation

Each execution attempt receives a new `activation_id` and, after runtime bind,
a fresh Hermes `subagent_id`.

Important states include:

- `STARTING`;
- `RUNNING`;
- `CANCEL_REQUESTED`;
- `SUCCEEDED`;
- `CANCELLED`;
- `ABANDONED`;
- terminal failure states.

The worker row acts as the cross-process serialization mutex: only one
reservation may move a worker from `DORMANT` to `RUNNING`.

### Task DAG

Tasks are optional orchestration metadata layered on top of workers.

Task states are:

- `pending`;
- `in_progress`;
- `completed`;
- `failed`;
- `cancelled`.

`ready` is derived, never persisted. A pending task is ready only when every
`blocked_by` task is completed.

Task dependencies are cycle-checked and edits use revision compare-and-swap.

## Durable activation flow

A normal worker run is reserved atomically:

1. verify the addressed worker belongs to the parent session;
2. require `DORMANT` worker state;
3. select the oldest parent `PENDING` message;
4. move the message to `PROCESSING`;
5. create an activation in `STARTING`;
6. move the worker to `RUNNING` and increment its revision;
7. commit;
8. launch the fresh Hermes subagent;
9. bind its `subagent_id` and mark the activation `RUNNING`;
10. reconcile the terminal result back to SQLite.

The reservation happens before the HTTP API returns `202`, so clients receive
the durable activation identifier immediately.

## Cold recovery

Activation rows record process ownership metadata only for stale-owner
detection. On startup, the store checks non-terminal activations whose owning
process no longer exists.

An abandoned activation is marked `ABANDONED`, its processing parent message is
requeued to `PENDING`, and a `RUNNING` worker is released to `DORMANT`.

PID/birth-marker data is never returned by public Durable Worker projections.

## Operator cancellation

Cancellation is intentionally not an optimistic state flip.

When the API has proof of a locally supervised live activation:

1. durable activation state becomes `CANCEL_REQUESTED`;
2. worker remains `RUNNING`;
3. parent message remains `PROCESSING`;
4. lifecycle cancellation is requested through the live handle;
5. only after the child reaches terminal `CANCELLED` does reconciliation move:
   - activation -> `CANCELLED`;
   - message -> `PENDING`;
   - worker -> `DORMANT`.

This prevents a second activation from overlapping a child that is still
unwinding.

A gateway/system drain does not write the operator cancellation marker. A child
cancelled by drain therefore remains fail-closed: worker and message become
`FAILED` and require recovery.

## Retry and recovery

A failed-worker retry uses revision compare-and-swap. It preserves the failed
activation as immutable audit history, requeues the failed parent message and
moves the worker back to `DORMANT`. The next run creates a new activation.

Task recovery extends the same principle: the task, worker and durable task
message are restored coherently, and redispatch reuses the same task message
while creating a new activation and subagent.

## Task dispatch

A READY task is not implemented as a client-side `enqueue` + `run` sequence.
One transaction validates and creates the task-correlated work reservation:

- task must be `pending` and revision must match;
- every dependency must be completed;
- a session-owned worker must be assigned and `DORMANT`;
- unrelated older pending worker inbox messages block dispatch;
- task message becomes `PROCESSING`;
- a new activation becomes `STARTING`;
- worker becomes `RUNNING`;
- task becomes `in_progress`;
- the task/message/activation relationship is recorded in `durable_worker_task_runs`.

The reservation is then executed by the same Hermes subagent lifecycle as a
normal Durable Worker run.

## SQLite schema and compatibility

H6 formalizes the already-qualified H5 layout as:

`PRAGMA user_version = 1`

Version 1 contains:

- `durable_workers`;
- `durable_worker_messages`;
- `durable_worker_activations`;
- `durable_worker_tasks`;
- `durable_worker_task_dependencies`;
- `durable_worker_task_runs`.

H1-H5 databases were unversioned (`user_version = 0`). H6 adopts them
non-destructively after validating required columns and foreign-key integrity.
A database with a future schema version is rejected read-only before schema
bootstrap can mutate it.

The v1 layout is intentionally H5-compatible, so executable rollback to the H5
implementation does not require a reverse database migration.

`audit_schema()` provides an explicit read-only structural,
`foreign_key_check`, and `quick_check` audit.

## API design

The API extension inherits Hermes' existing API-server listener, bearer
authentication, CORS and profile scope. It does not add a second listener.

The surface is session-scoped and includes:

- worker list/detail;
- bounded messages and activations;
- enqueue and run;
- bounded task reads/writes;
- authenticated SSE invalidation;
- operator summary;
- retry/cancel;
- task graph, edit, dependency add/remove, dispatch and recovery.

Object-level operations fail closed when the addressed worker/task does not
belong to the active session.

Control routes use exact request contracts and reject unsupported query
parameters.

## Security and privacy boundaries

Public projections deliberately exclude:

- owner PID and process birth markers;
- lifecycle handles;
- callbacks/threads;
- credentials and API keys;
- global cross-session activity that could reveal another session's work.

The browser-facing Harness never receives the Hermes API Bearer; it talks to a
same-origin server-side BFF.

## Capacity and bounded projections

Durable Worker runs and task dispatch share the same process-wide activation
capacity. Task dispatch cannot bypass the normal concurrency limit.

Browser/API projections are bounded. In particular, the public task graph is
limited to 100 task nodes, bounded relationship lists, and bounded latest-run
summary/error text. Readiness is still computed from the complete dependency
set before projection truncation.

## Plugin behavior

The API-server plugin is opt-in. When disabled, the factory returns the stock
`APIServerAdapter`.

The `durable-workers` tool plugin exposes the original command/tool surface for
agent-side operation and uses the same versioned store contract as the final
API adapter.

## Qualification lineage

See [Durable Workers qualification ledger](durable-workers-qualification-ledger.md)
for the H1-H6 lineage and behavior SHAs.

A phase is considered qualified only after the relevant repository tests and
required real-runtime/browser evidence pass. Generated tests alone are not
accepted as evidence.

## AI assistance disclosure

See [🤖 AI-assisted development](ai-assisted-development.md). The Durable
Workers contribution was developed with substantial AI assistance under human
direction, review and real-runtime qualification. This disclosure applies to
this contribution and does not characterize unrelated upstream Hermes code.
