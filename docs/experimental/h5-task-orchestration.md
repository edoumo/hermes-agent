# H5 Durable Task Orchestration

Status: `H5_TASK_ORCHESTRATION_STATUS=PASS`

Branch: `experimental/durable-workers-task-orchestration`

Qualified backend behavior SHA: `ea254053c82929fc44646b6cb4c8456498d5deb4`

H4 baseline: `ab693e298d76a9fd8d65f174870a7c88cf68c962`

Qualified plugin: `api-server-durable-workers 0.4.0`

## Objective

H5 turns Durable Worker task metadata into a real operational DAG without introducing a second execution engine. H1-H4 remain authoritative for durable worker identity, inbox, activation serialization, lifecycle, cancellation, retry, crash recovery, API authentication and SSE. H5 owns the task graph, CAS-safe task edits, dependency management, atomic READY dispatch and task-result reconciliation.

## Qualified behavior

The isolated real-runtime qualification completed with `H5_TASK_ORCHESTRATION_STATUS=PASS` on backend SHA `ea254053c82929fc44646b6cb4c8456498d5deb4` and Harness SHA `97f614f19cb71439028287ed87bc11679ebe76db`.

Backend tests: 45/45 PASS (25 H5 plus 20 H4 non-regression).

Python compilation: PASS.

Factory behavior: stock `APIServerAdapter` by default, `DurableWorkersTaskRecoveryAPIServerAdapter` only when Durable Workers API is explicitly enabled. No new listener is introduced.

Route table: PASS with 59 routes total, 10 H5 routes, no duplicates and no H5 DELETE/PUT/PATCH controls.

## DAG semantics

Task states remain `pending`, `in_progress`, `completed`, `failed`, `cancelled`.

`ready` remains derived. It is true only for a pending task whose blockers are all completed. H5 does not persist a separate READY state.

The qualification proved a four-node reference DAG with exact edges and automatic backend-driven progression:

- root task READY;
- direct dependents BLOCKED;
- final join task BLOCKED;
- root completion automatically makes the direct dependents READY;
- completion of both direct dependents automatically makes the final task READY.

Blocked dispatch is rejected before creating any message, activation or task-run row.

## CAS-safe task administration

Pending tasks support subject/description edit, worker assignment/unassignment and reassignment using `expected_revision`.

Stale revisions return conflict without mutation.

Dependency add/remove increments revision and cycle creation is rejected without adding the edge.

## Atomic task dispatch

Task dispatch validates session ownership, task revision, task state, blockers, assigned worker state and worker inbox ordering before reserving anything.

A successful dispatch atomically creates or reuses the task-correlated durable parent message, creates a new Durable Worker activation, moves the worker to RUNNING, moves the task to `in_progress`, and records the task/message/activation audit relation.

The reservation is then executed through the already-qualified Durable Worker lifecycle. H5 does not persist live lifecycle handles, AIAgent objects, callbacks, threads or sockets.

Real DeepSeek dispatch was qualified. Task, message, activation and worker moved through the expected durable states and reconciled to `completed` / `SUCCEEDED` / `CONSUMED` / `DORMANT` only after the child completed successfully.

## Cancellation and redispatch

H4 operator cancellation remains authoritative.

During `CANCEL_REQUESTED`, qualification proved atomically that task stays `in_progress`, worker stays `RUNNING` and message stays `PROCESSING`. No redispatch overlap occurs.

Only after terminal `CANCELLED` does the task return to `pending`, the worker to `DORMANT` and the task message to `PENDING`.

Redispatch reuses the same durable task message while creating a new activation id and new subagent id. The previous cancelled activation remains audit history.

## Fail-closed recovery

A system drain that does not use the operator-cancel route keeps H4 fail-closed semantics. Qualification proved worker `FAILED`, message `FAILED`, task `failed` and preserved failed activation history.

`Recover task` restores the failed task, worker and task-owned message atomically to `pending` / `DORMANT` / `PENDING` under revision CAS. Redispatch keeps the same message id and creates a new activation/subagent.

## Crash/restart recovery

A real lab backend was killed with SIGKILL while a task activation was running.

On restart, H1 recovered the activation to `ABANDONED`, the worker to `DORMANT` and the message to `PENDING`. H5 startup reconciliation moved the task from `in_progress` back to `pending` and recorded the recovered durable state in the task-run audit.

The task then redispatched with the same message id and a new activation id and completed successfully.

## Inbox ordering

H5 never overtakes an unrelated pending parent message already queued for the assigned worker. Dispatch is rejected and creates no task activation in that case.

## Public graph projection

The graph is session scoped and bounded to at most 100 task nodes. Unknown query parameters and limits above 100 are rejected.

Public task-run summary/error fields are bounded to 2000 characters. Display relation lists are bounded while readiness continues to be computed from all real dependencies.

The public graph excludes owner PID/birth markers, live handles, Bearer credentials and secrets.

## Capacity and isolation

H5 shares the existing Durable Worker process-wide activation capacity. With cap=1, a second H5 dispatch was correctly rejected with `429 durable_worker_capacity` while another activation was running and accepted after capacity was released.

Cross-session graph, edit, dependency, dispatch and recover accesses fail closed. Invalid query strings do not bypass object ownership semantics.

## Harness / SSE

The H5 Harness graph is session-level, uses backend-projected readiness and preserves H3's single EventSource ownership. No H5 JavaScript creates an EventSource or stores graph/transcript data in localStorage.

Real UI transitions for dispatch, completion, cancellation, recovery and redispatch occurred without manual refresh. DOM growth remained bounded and no SSE 429/reconnect storm was observed.

## Qualification evidence

Evidence directory:

`/home/edou/lab/hermes-durable-workers-h5/evidence-h5/`

Archive:

`/home/edou/lab/hermes-durable-workers-h5/evidence-h5/h5-task-orchestration-evidence.tar.gz`

SHA256:

`48706e3b31f39f4ae7fca88233ab787f7c1f7659d2b96b89438aef59537f0e43`

The evidence archive was scanned for lab keys/passwords and reported clean.

## Qualification boundary

The principal Hermes runtime, legacy WebUI and main configuration were not modified. Lab processes and ports were cleanly shut down.

One obsolete H4 static WebUI test was reported during qualification. It asserted a literal direct import of the H4 BFF from `harness_server.py`; H5 correctly layers through `harness_ui_task_recovery` which delegates to H5 tasks and H4 operations. This is a test-hygiene issue only and does not change this H5 behavior PASS.

No PR, merge, main/master update or principal runtime deployment is authorized by this qualification.
