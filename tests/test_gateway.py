"""Tests for Gateway API routes (/api/v1/agents)."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient
from contextlib import asynccontextmanager


def _create_gateway_test_app(registry=None, adapter_manager=None):
    """Create a test FastAPI app including the gateway router (no real lifespan)."""
    from fastapi import FastAPI
    from src.gateway.routes import router, set_deps

    if registry is not None or adapter_manager is not None:
        set_deps(registry, adapter_manager)

    @asynccontextmanager
    async def empty_lifespan(app):
        yield

    app = FastAPI(lifespan=empty_lifespan)
    app.include_router(router)
    return app


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_registry():
    reg = MagicMock()
    reg.register = AsyncMock(return_value="agent-001")
    reg.heartbeat = AsyncMock(return_value=10)
    reg.deregister = AsyncMock()
    reg.list_agents = AsyncMock(return_value=[
        {"agent_id": "agent-001", "name": "worker-a", "skills": ["code"]},
        {"agent_id": "agent-002", "name": "worker-b", "skills": ["design"]},
    ])
    reg.find_by_skill = AsyncMock(return_value=[
        {"agent_id": "agent-001", "name": "worker-a", "skills": ["code"]},
    ])
    return reg


@pytest.fixture
def mock_adapter_manager():
    mgr = MagicMock()
    mock_adapter = MagicMock()
    from src.adapters.base import AgentResult
    mock_adapter.invoke = AsyncMock(return_value=AgentResult(
        success=True, output="task completed",
    ))
    mgr.get = MagicMock(return_value=mock_adapter)
    return mgr


@pytest.fixture
def client(mock_registry, mock_adapter_manager):
    app = _create_gateway_test_app(mock_registry, mock_adapter_manager)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client_no_deps():
    """Client backed by an app where set_deps() was never called."""
    from src.gateway import routes
    # Reset module-level globals so endpoints see None deps
    routes._registry = None
    routes._adapter_manager = None
    app = _create_gateway_test_app()
    return TestClient(app, raise_server_exceptions=False)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_register_agent_success(client, mock_registry):
    """POST /register with valid data returns 200 with agent_id."""
    payload = {
        "name": "test-agent",
        "endpoint": "http://localhost:8001",
        "protocol": "http",
        "skills": ["code", "review"],
        "heartbeat_interval": 15,
    }
    resp = client.post("/api/v1/agents/register", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_id"] == "agent-001"
    assert data["heartbeat_interval"] == 15
    assert data["status"] == "registered"

    mock_registry.register.assert_awaited_once_with(
        name="test-agent",
        endpoint="http://localhost:8001",
        protocol="http",
        skills=["code", "review"],
        heartbeat_interval=15,
    )


def test_register_agent_defaults(client, mock_registry):
    """POST /register with only required fields uses defaults."""
    payload = {"name": "minimal-agent", "endpoint": "http://localhost:9000"}
    resp = client.post("/api/v1/agents/register", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_id"] == "agent-001"
    assert data["heartbeat_interval"] == 10  # default
    assert data["status"] == "registered"

    mock_registry.register.assert_awaited_once_with(
        name="minimal-agent",
        endpoint="http://localhost:9000",
        protocol="http",
        skills=[],
        heartbeat_interval=10,
    )


def test_register_agent_missing_name(client):
    """POST /register without name returns 422."""
    resp = client.post("/api/v1/agents/register", json={
        "endpoint": "http://localhost:8001",
    })
    assert resp.status_code == 422


def test_register_agent_missing_endpoint(client):
    """POST /register without endpoint returns 422."""
    resp = client.post("/api/v1/agents/register", json={
        "name": "test-agent",
    })
    assert resp.status_code == 422


def test_register_agent_empty_body(client):
    """POST /register with empty body returns 422."""
    resp = client.post("/api/v1/agents/register", json={})
    assert resp.status_code == 422


def test_register_agent_server_error(client, mock_registry):
    """POST /register when registry raises returns 500."""
    mock_registry.register.side_effect = RuntimeError("db down")
    resp = client.post("/api/v1/agents/register", json={
        "name": "test-agent",
        "endpoint": "http://localhost:8001",
    })
    assert resp.status_code == 500


def test_heartbeat_success(client, mock_registry):
    """POST /{agent_id}/heartbeat returns 200."""
    resp = client.post("/api/v1/agents/agent-001/heartbeat")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["next_heartbeat_in"] == 10

    mock_registry.heartbeat.assert_awaited_once_with("agent-001")


def test_heartbeat_unknown_agent(client, mock_registry):
    """POST /{agent_id}/heartbeat for unknown agent returns 404."""
    mock_registry.heartbeat.side_effect = KeyError("agent-999")
    resp = client.post("/api/v1/agents/agent-999/heartbeat")
    assert resp.status_code == 404
    assert "agent-999" in resp.json()["detail"]


def test_deregister_success(client, mock_registry):
    """POST /{agent_id}/deregister returns 200."""
    resp = client.post("/api/v1/agents/agent-001/deregister")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "deregistered"

    mock_registry.deregister.assert_awaited_once_with("agent-001")


def test_deregister_unknown_agent(client, mock_registry):
    """POST /{agent_id}/deregister for unknown agent returns 404."""
    mock_registry.deregister.side_effect = KeyError("agent-999")
    resp = client.post("/api/v1/agents/agent-999/deregister")
    assert resp.status_code == 404


def test_list_agents(client, mock_registry):
    """GET /agents returns the full list."""
    resp = client.get("/api/v1/agents")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["agent_id"] == "agent-001"

    mock_registry.list_agents.assert_awaited_once()
    mock_registry.find_by_skill.assert_not_awaited()


def test_list_agents_by_skill(client, mock_registry):
    """GET /agents?skill=code returns filtered list."""
    resp = client.get("/api/v1/agents?skill=code")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["agent_id"] == "agent-001"

    mock_registry.find_by_skill.assert_awaited_once_with("code")
    mock_registry.list_agents.assert_not_awaited()


def test_invoke_agent_success(client, mock_registry, mock_adapter_manager):
    """POST /{agent_id}/invoke returns AgentResult."""
    resp = client.post("/api/v1/agents/agent-001/invoke", json={
        "task": "write tests",
        "context": {"repo": "swarm"},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_id"] == "agent-001"
    assert data["success"] is True
    assert data["result"] == "task completed"
    assert data["error"] is None

    mock_adapter_manager.get.assert_called_once_with("agent-001")


def test_invoke_agent_not_found(client, mock_adapter_manager):
    """POST /{agent_id}/invoke for unknown agent returns 404."""
    mock_adapter_manager.get.return_value = None
    resp = client.post("/api/v1/agents/agent-999/invoke", json={
        "task": "do something",
    })
    assert resp.status_code == 404


def test_invoke_agent_missing_task(client):
    """POST /{agent_id}/invoke without task returns 422."""
    resp = client.post("/api/v1/agents/agent-001/invoke", json={})
    assert resp.status_code == 422


def test_invoke_agent_invoke_error(client, mock_adapter_manager):
    """POST /{agent_id}/invoke when adapter.invoke fails returns 500."""
    mock_adapter = MagicMock()
    mock_adapter.invoke = AsyncMock(side_effect=RuntimeError("adapter error"))
    mock_adapter_manager.get = MagicMock(return_value=mock_adapter)

    resp = client.post("/api/v1/agents/agent-001/invoke", json={
        "task": "fail task",
    })
    assert resp.status_code == 500


def test_deps_not_set(client_no_deps):
    """Any endpoint when deps are not set returns 503."""
    # Test on multiple endpoints to be thorough
    endpoints = [
        ("POST", "/api/v1/agents/register", {"name": "x", "endpoint": "http://x"}),
        ("POST", "/api/v1/agents/agent-001/heartbeat", None),
        ("POST", "/api/v1/agents/agent-001/deregister", None),
        ("GET", "/api/v1/agents", None),
        ("POST", "/api/v1/agents/agent-001/invoke", {"task": "x"}),
    ]
    for method, path, payload in endpoints:
        if payload is not None:
            resp = client_no_deps.request(method, path, json=payload)
        else:
            resp = client_no_deps.request(method, path)
        assert resp.status_code == 503, (
            f"{method} {path} should return 503 when deps not set, got {resp.status_code}"
        )
        assert "not available" in resp.json()["detail"].lower()
