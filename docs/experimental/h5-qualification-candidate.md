# H5 qualification candidate

Status: `H5_BACKEND_CODE_READY_FOR_INTEGRATED_QUALIFICATION`

Branch: `experimental/durable-workers-task-orchestration`

H4 parent: `ab693e298d76a9fd8d65f174870a7c88cf68c962`

Plugin: `api-server-durable-workers 0.4.0`

This candidate contains the complete first H5 task-orchestration slice:

- bounded session-scoped task graph projection;
- pending task edit/reassignment with revision CAS;
- dependency add/remove with CAS and cycle rejection;
- atomic READY task dispatch into the existing Durable Worker lifecycle;
- shared H2.1/H4 activation capacity and serialization;
- task run audit linking task/message/activation/worker durable ids;
- automatic success/failure/operator-cancel reconciliation;
- task-aware failure recovery and redispatch using the same durable message with a fresh activation;
- startup reconciliation after H1 abandoned-work recovery;
- bounded public graph text/relation payloads;
- no new listener, auth system, scheduler, process model or live-handle persistence.

The authoring environment cannot execute repository pytest because it cannot resolve `github.com`; no local integrated PASS is claimed. Tests are committed for the H5 core, API, recovery, crash reconciliation, public payload bounds and platform factory. Real runtime qualification must run in an isolated lab.

No PR, merge or principal runtime mutation is authorized.
