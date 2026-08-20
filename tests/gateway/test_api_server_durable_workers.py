"""Contract tests for the experimental H2 Durable Workers API adapter."""

from gateway.platforms.api_server_durable import DurableWorkersAPIServerAdapter


def test_durable_worker_routes_extend_native_api_table():
    adapter = object.__new__(DurableWorkersAPIServerAdapter)
    routes = {(method, path) for method, path, _handler in adapter._http_route_table()}

    # Existing Hermes session/run surfaces remain present.
    assert ("GET", "/api/sessions") in routes
    assert ("GET", "/api/sessions/{session_id}") in routes
    assert ("POST", "/v1/runs") in routes

    # H2 adds only session-scoped read routes at this checkpoint.
    assert ("GET", "/api/sessions/{session_id}/workers") in routes
    assert ("GET", "/api/sessions/{session_id}/workers/{worker_id}") in routes
    assert (
        "GET",
        "/api/sessions/{session_id}/workers/{worker_id}/messages",
    ) in routes
    assert (
        "GET",
        "/api/sessions/{session_id}/workers/{worker_id}/activations",
    ) in routes
    assert ("GET", "/api/sessions/{session_id}/worker-tasks") in routes

    assert not any(
        method != "GET" and ("/workers" in path or "/worker-tasks" in path)
        for method, path in routes
    )


def test_durable_worker_route_names_are_unique():
    adapter = object.__new__(DurableWorkersAPIServerAdapter)
    routes = [(method, path) for method, path, _handler in adapter._http_route_table()]
    assert len(routes) == len(set(routes))


def test_h2_adapter_does_not_add_a_second_listener_contract():
    # The extension is a subclass of the existing API server; it has no
    # connect/listener implementation of its own. Listener/auth/CORS behavior
    # therefore remains owned by APIServerAdapter.
    assert "connect" not in DurableWorkersAPIServerAdapter.__dict__
    assert "_check_auth" not in DurableWorkersAPIServerAdapter.__dict__
    assert "_origin_allowed" not in DurableWorkersAPIServerAdapter.__dict__
