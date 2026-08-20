# H1 Durable Workers Final Report

## Executive summary

H1 has reached an architecture-validated experimental implementation checkpoint with an additional concurrency/crash-recovery hardening pass completed before real-machine validation.

The code proves the intended separation between durable worker identity and short-lived Hermes subagent activations without replacing Hermes delegation, changing the canonical Hermes database, adding DeepSeek Harness as a dependency, or touching production runtime state.

## Final status

`H1_PARTIAL_ARCHITECTURE_VALIDATED`

The remaining gate is an isolated real-Hermes / real-model cold-reactivation recipe. H1 must not be called complete before that recipe succeeds.

## Git state

Branch: `experimental/durable-workers`

Base: upstream Hermes Agent commit `c47f0b4590e6b5bb05fb73a42f447ca5444f5188` (2026-08-20)

The branch was intentionally based on current upstream rather than the stale `edoumo/hermes-agent` default branch. Existing MITC custom commits will need an explicit rebase/porting decision later; they were not rewritten by H1.

Upstream moved further during H1 development. The post-base delta was reviewed and did not require an immediate rebase for the H1 files. No merge and no pull request have been performed.

## Architecture retained

`DurableWorkerStore` owns durable worker identity, inbox/transcript, activation ledger, reports and a minimal task DAG.

`DurableWorkerService` resolves the active parent, enforces parent ownership and delegates execution to the existing public Hermes `SubagentLifecycleService`.

The bundled `durable-workers` plugin is the first consumer and is opt-in.

## Architecture rejected

* no serialized `AIAgent`;
* no second agent loop;
* no replacement of `delegate_tool`;
* no DeepSeek Harness runtime dependency;
* no Redis/Kafka/PostgreSQL service;
* no canonical `state.db` migration in H1;
* no UI reading SQLite directly.

## DurableWorker contract

A worker contains stable `worker_id`, parent session ownership, label, role, optional model/toolsets, status, revision and last activation reference.

The defining invariant is:

`worker_id != activation_id != subagent_id`

## Activation lifecycle

Reservation is atomic. One SQLite `BEGIN IMMEDIATE` transaction moves a worker to `RUNNING`, moves exactly one pending message to `PROCESSING`, and inserts the activation record. This makes the worker state the cross-process serialization boundary and prevents two Hermes processes from activating the same durable worker concurrently.

H1 then launches a fresh Hermes subagent via the public lifecycle API, waits for the terminal result, persists the report and returns the durable worker to `DORMANT` on success.

Timeouts fail closed: H1 requests cancellation but keeps the worker locked in `RUNNING` with the activation marked `CANCEL_REQUESTED` until process-loss recovery can prove the owner disappeared. It never unlocks immediately into a potentially overlapping activation.

If a process disappears during a live activation, startup recovery marks that activation `ABANDONED` and requeues the processing message.

## Process identity safety

Live activation ownership persists both PID and the Hermes process-start marker. Crash recovery therefore detects PID reuse rather than assuming a recycled PID still owns an old activation.

## Persistence

H1 uses `HERMES_HOME/durable-workers.db`, SQLite with foreign keys and WAL where available.

This database is intentionally separate from canonical Hermes state for the experimental phase.

## Inbox and reporting

Parent messages have stable message IDs and idempotent enqueue behavior. A reused ID with different durable content is rejected.

Successful worker summaries are persisted as worker-direction transcript entries and activation summaries.

## Authorization model

Workers and tasks are scoped to `parent_session_id`. Cross-parent worker and task access fails closed.

No credentials, live agent instances, callbacks, sockets, threads or LLM clients are persisted.

## Task DAG

H1 implements a deliberately small DAG primitive with optional worker ownership, `blocked_by`, derived readiness, cycle rejection and integer revision compare-and-set updates.

## Test results

Local isolated H1 logic tests after hardening: `8 passed`.

Covered properties include:

* durable identity across store/service reconstruction;
* distinct activation and subagent across cold service reconstruction;
* prior transcript continuity;
* cross-parent authorization denial;
* inbox idempotency and conflict handling;
* atomic worker/message/activation reservation;
* two process-like claimers cannot overlap the same worker;
* timeout keeps the worker locked;
* abandoned activation recovery and message requeue;
* PID-reuse-safe owner detection path;
* task readiness;
* cycle rejection;
* stale-revision rejection.

## CI

No PR was authorized and the current upstream CI runs on pull requests or pushes to `main`. Therefore there is no branch CI verdict at this checkpoint.

## Production impact

None.

No `srv-hermes` change, service restart, systemd change, profile change, plugin enablement, database migration, firewall change, network change, or production deployment occurred.

## Compatibility

H1 consumes the public Hermes subagent lifecycle contract rather than private child registries. This maximizes compatibility with current delegation safety and avoids a forked child-agent implementation.

The plugin remains disabled unless explicitly enabled through Hermes plugin configuration.

## DeepSeek Harness concepts adopted

Conceptually adopted:

* durable identity separate from activation;
* continuable worker through fresh activation;
* durable inbox;
* explicit parent relationship;
* durable reports;
* small dependency graph;
* UI/API separation.

Not adopted:

* DeepSeek runtime;
* Cordis dependency;
* DSH session format;
* DSH memory stack;
* DSH web server as a Hermes dependency.

## Hermes-Harness-UI consequence

The H1 service is intentionally UI agnostic. The next UI-facing stage should expose bounded, typed Hermes APIs around workers, messages, activation history and tasks. Hermes-Harness-UI should consume those APIs and incremental events rather than import Hermes runtime code or read SQLite.

## Known limitations

* real-model cold reactivation has not yet been exercised on `srv-hermes`;
* branch CI has not run because no PR/main push was authorized;
* H1 is activation-level continuation, not mid-tool/mid-token checkpointing;
* transcript continuation currently uses a bounded textual context rather than native persisted child-session replay;
* worker task DAG is intentionally minimal;
* the current experimental store is a separate database and is not yet part of canonical backup/migration policy.

## H2 recommendation

Proceed to the isolated real-machine recipe before deciding H2.

If the real recipe passes, H2 should focus on:

1. typed Gateway/API operations for durable workers;
2. pagination and incremental event delivery suitable for Hermes-Harness-UI;
3. optional consolidation with canonical Hermes state;
4. operational observability and quotas;
5. explicit migration/porting of the MITC Hermes custom commits onto the chosen modern upstream base.

## Actions requiring Ed decision

The next execution step is an isolated installation/test of this branch on the Hermes host. That step requires infrastructure access and should therefore be executed by Diane under a narrowly scoped mandate while the active Hermes runtime remains untouched.

No merge should occur before that validation.
