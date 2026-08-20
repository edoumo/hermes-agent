# H1 Durable Workers Test Matrix

## Automated H1 logic tests

| Capability | Test | Expected |
|---|---|---|
| Durable identity | service/store reconstruction then follow-up | same `worker_id`, new activation and new subagent |
| Context continuity | second activation context | contains first parent message and first worker report |
| Parent isolation | second parent reads/sends to foreign worker | fail closed |
| Inbox idempotency | same `message_id` and same content | no duplicate |
| Inbox conflict | same `message_id`, different content | conflict |
| Atomic reservation | two process-like claimers race on one worker | exactly one `RESERVED`, the other `BUSY` |
| Worker serialization | second message exists while first activation owns worker | only one `PROCESSING` message |
| Timeout safety | live activation times out | cancellation requested, worker remains locked `RUNNING` |
| Abandoned activation | simulated owner process loss | activation `ABANDONED`, message requeued, worker `DORMANT` |
| PID reuse safety | process identity marker no longer matches | activation treated as abandoned |
| DAG readiness | B and C blocked by A | blocked until A completed |
| Cycle protection | A blocked by descendant D | rejected |
| Optimistic concurrency | stale task revision update | rejected |

Local isolated result during H1 hardening: **8 passed**.

The tests exercise the actual H1 store/service code while replacing the public Hermes subagent lifecycle with a contract-compatible fake. They do not claim real-model or full-repository integration coverage.

## Concurrency invariant

One durable worker may have at most one live activation across cooperating Hermes processes sharing the same H1 database. Reservation is performed in one `BEGIN IMMEDIATE` SQLite transaction that moves the worker to `RUNNING`, the selected inbox message to `PROCESSING`, and inserts the activation record atomically.

A timeout does not immediately unlock the worker. H1 records `CANCEL_REQUESTED` and leaves the worker `RUNNING` until the owning process is proven gone and startup recovery marks the activation `ABANDONED`. This fails closed against overlapping work.

## Process identity invariant

Crash recovery persists both the owner PID and the process start marker used by Hermes. This prevents a recycled PID from making an abandoned activation appear live.

## Static contract checks performed

The H1 launch request was compared against the current upstream public `SubagentLaunchRequest` contract. H1 uses supported fields: `goal`, `context`, `role`, `model`, `allowed_toolsets`, `parent_session_id`, `correlation_id`, `metadata`, and `timeout_seconds`.

The plugin architecture was checked against the current `PluginContext` surface, which provides `subagent_lifecycle`, `register_tool`, `register_command`, and tool dispatch facilities.

## CI status

The upstream CI orchestrator runs on pull requests and pushes to `main`. The H1 branch is intentionally not a PR yet and `main` has not been modified, so no branch CI result is available at this checkpoint.

## Required real-machine recipe before H1 COMPLETE

1. install/check out `experimental/durable-workers` in an isolated environment on the Hermes host;
2. do not replace the active production Hermes installation;
3. explicitly enable `durable-workers` only in the isolated profile;
4. create a durable worker and send a first real task;
5. record worker ID, activation ID, subagent ID and report;
6. terminate the isolated Hermes process after the completed turn;
7. start a fresh isolated process against the same HERMES_HOME test state;
8. send a follow-up to the same worker ID;
9. verify a new activation and a new subagent are created and the response uses prior durable context;
10. verify another parent session cannot operate the worker;
11. exercise a controlled process-loss recovery and confirm requeue;
12. verify `/workers show <id>` and task listing;
13. disable the plugin and confirm ordinary Hermes behavior is unchanged.
