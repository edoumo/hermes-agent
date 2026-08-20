# ADR H1: Hermes Durable Workers

Status: accepted for experimental H1 implementation

## Decision

Represent a durable worker as persistent identity and bounded durable transcript, separate from any live Hermes subagent activation.

The H1 implementation uses:

* the existing public `SubagentLifecycleService` for execution;
* a separate `durable-workers.db` SQLite store for the experiment;
* parent-session ownership as the initial authorization boundary;
* a fresh subagent activation for every processed durable inbox message;
* durable reports written back to the worker transcript;
* a minimal durable task DAG with cycle rejection and optimistic revisions;
* an opt-in bundled plugin as the first consumer.

The central invariant is:

`worker_id != activation_id != subagent_id`

## Why this shape

Hermes already has a mature delegation runtime. Replacing it would duplicate safety, approvals, tool restrictions, child construction, cancellation, cost aggregation and lifecycle hooks.

The missing H1 primitive is durability above that runtime. A worker identity must survive when the particular Python child object does not.

## Persistence decision

H1 deliberately does not migrate the canonical Hermes `state.db`. It creates `durable-workers.db` under `HERMES_HOME`.

This keeps the prototype reversible and prevents an experimental schema from becoming an accidental production contract. If H2 is approved, consolidation into canonical Hermes state can be evaluated with a real migration plan.

## Cold reactivation

Cold reactivation means a later activation can continue the same durable worker using its bounded transcript. It does not mean checkpointing a live interpreter, resuming a half-completed tool call, or serializing `AIAgent`.

If an activation owner disappears, H1 marks the activation abandoned and requeues its processing message for a future activation.

## Authorization

Every worker and task is scoped by `parent_session_id`. An operation that presents a worker or task belonging to another parent fails closed.

The H1 store never persists provider credentials, API keys, callbacks, sockets, threads, LLM clients or live agent objects.

## Alternatives considered

### Extend `delegate_tool` directly

Rejected for H1. It would mix experimental durable state with a mature execution tool and increase regression risk.

### Persist serialized AIAgent objects

Rejected. Runtime objects are not a stable storage contract and would risk secrets, stale clients, callbacks, sockets and incompatible process state.

### Reuse DeepSeek Harness as the worker runtime

Rejected. H1 adopts useful concepts but adds no DeepSeek Harness runtime dependency.

### Put H1 directly into Hermes `state.db`

Deferred. Canonical-state integration is an H2 decision after real-machine validation.

### Build a separate worker daemon

Rejected for H1. Existing Hermes subagent lifecycle already provides the execution capability.

## UI/API consequence

`DurableWorkerService` is deliberately UI agnostic. Future Hermes-Harness-UI must consume typed Hermes API operations rather than access SQLite directly.

The desired future layering is:

`Hermes-Harness-UI -> Hermes API -> DurableWorkerService -> SubagentLifecycleService`

## H2 gate

H2 is recommended only after a real Hermes recipe proves:

1. first activation succeeds with a real model;
2. Hermes process can disappear;
3. second activation uses the same worker identity;
4. prior durable context is understood;
5. parent isolation remains enforced;
6. the operational overhead is acceptable.
