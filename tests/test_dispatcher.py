"""R2.1 tests: Dispatcher protocol + dispatch dataclasses.

These pin the contract that Round 2 builds on: the data shapes that flow through
the unified dispatch path (Docker containers + external agents as one candidate pool).
"""
from src.dispatcher.base import (
    Dispatcher,
    DispatchAttempt,
    DispatchRequest,
    DispatchResult,
    DispatchTarget,
)


def test_dispatch_target_docker():
    t = DispatchTarget(kind="docker", agent_type="frontend-ux-pro")
    assert t.kind == "docker"
    assert t.agent_type == "frontend-ux-pro"
    assert t.agent_id is None
    assert t.endpoint is None


def test_dispatch_target_external():
    t = DispatchTarget(kind="external", agent_type="x", agent_id="a1", endpoint="http://a:9")
    assert t.kind == "external"
    assert t.agent_id == "a1"
    assert t.endpoint == "http://a:9"


def test_dispatch_request_defaults():
    r = DispatchRequest(agent_type="backend-engineer", task="build api")
    assert r.task == "build api"
    assert r.context == {}
    assert r.timeout is None


def test_dispatch_result_defaults():
    res = DispatchResult(success=False)
    assert res.output == ""
    assert res.artifacts == []
    assert res.attempts == []
    assert res.target is None
    assert res.error is None


def test_dispatch_attempt_carries_target():
    t = DispatchTarget(kind="docker", agent_type="g")
    a = DispatchAttempt(target=t, success=False, error="boom")
    assert a.target is t
    assert a.success is False
    assert a.error == "boom"


def test_dispatcher_protocol_shape():
    # Dispatcher is a structural protocol; any object exposing async dispatch qualifies.
    assert hasattr(Dispatcher, "dispatch")
