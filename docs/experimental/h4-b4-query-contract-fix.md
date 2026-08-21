# H4 B4 query-contract correction

Status: `H4_B4_ORDERING_FIX_READY_FOR_TARGETED_REQUALIFICATION`

Initial H4 integrated qualification on backend code `6c2d3d5ac13d93b70dfcb14546afcacc8453db90` validated every real cancel, retry, recovery, SSE, isolation and UI capability, but returned `H4_OPERATIONAL_RECOVERY_STATUS=PARTIAL` because the three H4 control routes accepted and ignored query parameters.

The first B4 correction on `cca1aab336331857f36a6655a2b97627d02e3ada` correctly rejected all query parameters with HTTP 400 and zero observed mutation. Its targeted lab requalification found one remaining ordering mismatch: a valid session B addressing a worker owned by A plus a query string returned 400 before the worker ownership lookup, whereas the no-query form correctly returned the fail-closed 404.

No durable state, cancellation, retry, lifecycle, SSE or secret-boundary failure was observed. The remaining issue was validation ordering only.

## Final corrected contract

The H4 backend rejects every non-empty query string on these routes:

* `GET /api/sessions/{session_id}/worker-operations`
* `POST /api/sessions/{session_id}/workers/{worker_id}/retry`
* `POST /api/sessions/{session_id}/workers/{worker_id}/activations/{activation_id}/cancel`

Ordering is now explicit:

* `worker-operations`: session lookup -> query guard -> operational read;
* `retry`: session lookup -> read-only worker ownership projection -> query guard -> body -> durable mutation;
* `cancel`: session lookup -> read-only worker ownership projection -> query guard -> body -> activation/lifecycle mutation.

The worker ownership lookup uses the qualified H2 `DurableWorkersProjection`, which opens SQLite in `mode=ro` and enables `PRAGMA query_only=ON`. An invalid query therefore cannot construct the mutable H1 store or trigger abandoned-activation recovery as a side effect.

Consequences:

* valid session + owned worker + query => HTTP 400 `invalid_durable_worker_request`;
* valid session + foreign worker + query => HTTP 404 `durable_worker_not_found`;
* nonexistent/foreign session + query => existing session 404;
* no request body is read before the query guard;
* no lifecycle handle is accessed and no durable mutation is attempted before the guard.

## Regression coverage

`tests/gateway/test_api_server_durable_workers_control_queries.py` covers:

* empty query accepted;
* non-empty `query_string` rejected;
* request doubles exposing only `.query` rejected;
* operations query rejected before operational-store read;
* retry query performs only read-only ownership lookup before returning 400;
* retry foreign worker plus query remains 404;
* cancel query performs only read-only worker ownership lookup before returning 400;
* cancel foreign worker plus query remains 404;
* invalid-query paths explicitly fail if the mutable store, body parser or control mutation path is reached.

The experimental plugin version is `0.3.2` for this final B4 ordering candidate.

## Requalification scope

The previous H4 real-runtime evidence remains authoritative for all unaffected tracks. The final lab gate should be limited to:

1. exact new backend HEAD and plugin `0.3.2`;
2. targeted query-ordering tests plus existing H4 control tests;
3. real HTTP 400 on the three routes for owned objects plus query;
4. real cross-session worker + query => 404 on retry and cancel;
5. zero state mutation for rejected query requests;
6. one no-query smoke for operations, retry and cancel;
7. principal runtime/config unchanged.

A full replay of the real DeepSeek cancellation/retry matrix is unnecessary unless the targeted smoke reveals a regression.
