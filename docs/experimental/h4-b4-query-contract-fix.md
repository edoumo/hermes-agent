# H4 B4 query-contract correction

Status: `H4_B4_FIX_READY_FOR_TARGETED_REQUALIFICATION`

Initial H4 integrated qualification on backend code `6c2d3d5ac13d93b70dfcb14546afcacc8453db90` validated every real cancel, retry, recovery, SSE, isolation and UI capability, but returned `H4_OPERATIONAL_RECOVERY_STATUS=PARTIAL` because the three H4 control routes accepted and ignored query parameters.

B4 affected only strict request-shape validation. No durable state, cancellation, retry, lifecycle, SSE, session-isolation or security-boundary failure was observed.

## Corrected contract

The H4 backend now rejects every non-empty query string on these routes:

* `GET /api/sessions/{session_id}/worker-operations`
* `POST /api/sessions/{session_id}/workers/{worker_id}/retry`
* `POST /api/sessions/{session_id}/workers/{worker_id}/activations/{activation_id}/cancel`

Rejection occurs only after the existing session authorization/lookup step. Therefore a foreign or nonexistent session still fails closed through the existing session-scoped 404 behavior instead of leaking route validation details.

For an authorized session, query parameters return HTTP 400 with error code:

`invalid_durable_worker_request`

The guard runs before control-store access, request-body processing or lifecycle mutation.

## Regression coverage

`tests/gateway/test_api_server_durable_workers_control_queries.py` covers:

* empty query accepted;
* non-empty `query_string` rejected;
* request doubles exposing only `.query` rejected;
* operations query rejected before operational-store read;
* retry query rejected before body read or mutation;
* cancel query rejected before body read or lifecycle/control lookup.

The experimental plugin version is `0.3.1` for the corrected qualification candidate.

## Requalification scope

The previous H4 real-runtime evidence remains authoritative for all unaffected tracks. The follow-up lab gate should be limited to:

1. exact new backend HEAD and plugin `0.3.1`;
2. targeted B4 tests plus the existing H4 control API tests;
3. real HTTP 400 for query strings on all three routes;
4. one no-query smoke for operations, retry and cancel behavior to prove no regression;
5. cross-session 404 remains fail closed even when a query string is present;
6. principal runtime/config remains untouched.

A full replay of the real DeepSeek cancellation/retry matrix is unnecessary unless the targeted smoke reveals a regression.
