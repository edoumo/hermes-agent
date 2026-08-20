# H1 Durable Workers Final Report

## Executive summary

H1 Durable Workers has passed the isolated real-runtime recipe on `srv-hermes` with the real configured model `deepseek-v4-flash:0731`.

The recipe proved that a durable worker identity survives process disappearance, resumes through a new Hermes process, receives a distinct activation and subagent identity, and functionally reuses its persisted transcript. Concurrency serialization, crash recovery, task DAG semantics and parent isolation also passed on the real H1 SQLite store.

## Final status

`H1_REAL_RUNTIME_RECIPE_PASS`

## Git state

Branch: `experimental/durable-workers`

Historical branch base: `c47f0b4590e6b5bb05fb73a42f447ca5444f5188`

Real-runtime recipe tested H1 HEAD: `14c9856e15f45f1fcd877ac962f9cbc1e5e56aeb`

Post-recipe plugin contract cleanup removes the unsupported per-launch timeout field from the public `durable_worker` tool surface. No merge and no pull request have been performed.

## Real-runtime recipe

Environment:

* H1 home: `/home/edou/lab/hermes-durable-workers-h1/hermes-home`
* H1 virtualenv: `/home/edou/lab/hermes-durable-workers-h1/venv`
* model: `deepseek-v4-flash:0731`
* plugin: `durable-workers`
* primary Hermes runtime touched: `NO`
* primary Hermes home touched: `NO`

Targeted validation:

* H1 tests: `8/8 PASS`
* public subagent lifecycle tests: `4/4 PASS`
* plugin loading suite: `180 PASS` after installing the required `pytest-asyncio` test dependency in the isolated lab

## Cold resume proof

Stable worker identity:

`dw_1a91f9594cb9442fab831b84a12c825d`

Activation 1:

* activation: `dwa_5c1ffb5ed046403fac7aba983e320e46`
* subagent: `sa-0-bb084d8b`

The first H1 process then disappeared.

Activation 2 ran in a new Hermes process:

* activation: `dwa_c6c38586669747ed856b2837d14f05b5`
* subagent: `sa-0-8b1dd797`

The second activation was not given the original marker again, yet returned the exact marker `H1-COLD-RESUME-20260820T214341` from the durable transcript and explicitly confirmed context presence. This is functional continuity evidence, not merely evidence that the text existed in SQLite.

Validated invariants:

`worker_id_1 == worker_id_2`

`activation_id_1 != activation_id_2`

`subagent_id_1 != subagent_id_2`

`worker_id != activation_id != subagent_id`

## Concurrency serialization

Two independent process-like claimers used the same durable worker database with two pending messages.

Observed result:

* exactly one activation reservation succeeded;
* the competing claimant received `BUSY`;
* one message became `CONSUMED`;
* one remained `PENDING`;
* only one subagent activation existed for the worker.

The worker-level SQLite reservation is therefore a valid cross-process serialization boundary for H1.

## Crash recovery

A dedicated experimental process was killed during a long activation.

On restart:

* the previous activation became `ABANDONED`;
* its `PROCESSING` message returned to `PENDING`;
* the worker returned to `DORMANT`;
* a subsequent `run_next` created a new activation and completed successfully.

The main Hermes runtime was not affected.

## Task DAG and parent isolation

The real H1 store validated:

* A -> B/C -> D readiness transitions;
* cycle rejection;
* stale revision compare-and-set rejection;
* cross-parent read and write denial.

## Production isolation

The recipe did not modify the primary Hermes runtime.

Observed primary process identities remained intact and the primary `config.yaml` SHA256 was unchanged before and after the recipe.

No systemd mutation, network mutation, firewall mutation, production deployment, merge or pull request occurred.

## Contract findings from the real recipe

### Timeout semantics

The current Hermes `SubagentLifecycleService` exposes `timeout_seconds` on `SubagentLaunchRequest` for contract completeness but explicitly rejects per-launch timeout in `_validate_request()`.

Durable Workers therefore must not expose this as a child-launch option. The H1 plugin surface has been cleaned up accordingly. If H2 needs a deadline, it should be modeled as a host-side wait/cancel deadline rather than a delegated launch timeout.

### Toolset narrowing

A durable worker's requested toolsets must be a subset of the active parent Hermes toolsets. Hermes correctly rejects attempts that would broaden parent permissions.

H2/API documentation must preserve this as a security invariant, not work around it.

### Stable parent identity

Cold resume is scoped by `parent_session_id`. A restart therefore requires a stable named parent session identity. In the recipe this was achieved with the named session `h1-recipe`.

A volatile unnamed session receives a new parent identity and cannot address the previous worker. H2 should make this requirement explicit and consider introducing an explicit durable parent/workspace identity instead of depending on an incidental runtime session identifier.

### Test environment dependency

The full plugin loading suite requires `pytest-asyncio`. Missing it caused environment/setup failures rather than H1 functional failures. After installation in the isolated virtualenv, all 180 plugin-loading tests passed.

## Architecture retained

H1 keeps the established architecture:

* persistent worker identity separated from activation;
* fresh Hermes subagent activation for every turn;
* durable inbox and transcript;
* parent-scoped authorization;
* atomic worker/message/activation reservation;
* process-loss recovery using PID plus process-start marker;
* separate experimental `durable-workers.db`;
* minimal task DAG;
* UI-independent service boundary.

H1 still does not serialize `AIAgent`, credentials, clients, threads, sockets or callbacks.

## Known boundaries

H1 is activation-level continuation. It does not checkpoint an LLM generation or tool call mid-execution.

The transcript is currently reconstructed as bounded textual context rather than native persisted child-session replay.

The separate H1 SQLite store is still experimental and is not yet part of canonical Hermes backup/migration policy.

The durable parent identity contract needs to be formalized in H2.

## Evidence

Evidence directory:

`/home/edou/lab/hermes-durable-workers-h1/evidence/`

Archive:

`/home/edou/lab/hermes-durable-workers-h1/evidence/h1-real-runtime-recipe-evidence.tar.gz`

SHA256:

`f35904be6a359cbe9175899016c27cf1b69f658f91af1b429b6b630d27d15e63`

## H2 recommendation

H1 has cleared its real-runtime gate. H2 may now start.

Recommended H2 scope:

1. a typed Hermes API around durable workers, activations, messages and tasks;
2. stable durable parent/workspace identity;
3. pagination for worker transcripts and activation history;
4. incremental event delivery for Hermes Harness UI;
5. explicit observable fields such as last error, current activity, duration, usage and cost where Hermes can provide them safely;
6. capability-preserving toolset validation at the API boundary;
7. no UI direct access to SQLite;
8. no public exposure of the stock runtime API without authentication and authorization.

## Final verdict

`H1_REAL_RUNTIME_RECIPE_PASS`

H1 has demonstrated durable identity, true process-boundary cold resume, functional context continuity, concurrency serialization, crash recovery, task DAG behavior and parent isolation on the real Hermes host while leaving the active runtime intact.
