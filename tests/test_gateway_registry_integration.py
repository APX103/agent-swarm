"""R1.1 integration test: gateway ↔ REAL AgentRegistry contract.

Wires the production ``AgentRegistry`` (backed by the in-memory FakeRedis reused
from test_registry.py) into the gateway routes and asserts the full
register → heartbeat → deregister lifecycle works against the *real* registry
signatures:

- ``register(agent_data: dict) -> str``
- ``heartbeat(agent_id) -> bool``        (True=renewed, False=unknown)
- ``deregister(agent_id) -> None``       (safe no-op for unknown)

The pre-existing unit tests (test_gateway.py) and e2e tests (test_e2e_external_agent.py)
encoded a *different* (kwargs / KeyError / int) contract that the real AgentRegistry
does not implement — so the gateway would 500 / misbehave in production. This file
pins the real contract and drives the R1.1 fix.
"""
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.gateway.routes import router, set_deps
from src.registry.registry import AgentRegistry
from tests.test_registry import FakeRedis  # reuse the in-memory redis fake

BASE = "/api/v1/agents"


@pytest.fixture
def gw_client():
    """Gateway TestClient backed by the REAL AgentRegistry + FakeRedis."""
    registry = AgentRegistry(
        redis_url="redis://localhost:6379/0",
        heartbeat_ttl=30,
        heartbeat_interval=10,
    )
    registry._redis = FakeRedis({})  # bypass connect(); inject fake

    app = FastAPI()
    app.include_router(router)
    set_deps(registry, None)  # adapter_manager not needed for lifecycle tests
    client = TestClient(app, raise_server_exceptions=False)
    yield client


class TestGatewayRealRegistryLifecycle:
    def test_register_then_list(self, gw_client):
        resp = gw_client.post(f"{BASE}/register", json={
            "name": "real-agent",
            "endpoint": "http://localhost:9001",
            "protocol": "http",  # register-only; this test covers the registry lifecycle
            "skills": ["python", "code-review"],
        })
        assert resp.status_code == 200, resp.text
        agent_id = resp.json()["agent_id"]
        listed = gw_client.get(BASE).json()
        assert any(a["id"] == agent_id for a in listed)

    def test_heartbeat_ok_returns_default_interval(self, gw_client):
        aid = gw_client.post(f"{BASE}/register", json={
            "name": "hb", "endpoint": "http://x",
        }).json()["agent_id"]
        resp = gw_client.post(f"{BASE}/{aid}/heartbeat")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        # real registry.heartbeat returns bool, not an int interval — gateway must
        # surface a sane default, not leak True/False coerced to 1/0.
        assert body["next_heartbeat_in"] == 10

    def test_heartbeat_unknown_agent_404(self, gw_client):
        resp = gw_client.post(f"{BASE}/nope/heartbeat")
        assert resp.status_code == 404

    def test_deregister_then_gone(self, gw_client):
        aid = gw_client.post(f"{BASE}/register", json={
            "name": "del", "endpoint": "http://x",
        }).json()["agent_id"]
        resp = gw_client.post(f"{BASE}/{aid}/deregister")
        assert resp.status_code == 200, resp.text
        listed = gw_client.get(BASE).json()
        assert not any(a["id"] == aid for a in listed)

    def test_deregister_unknown_agent_404(self, gw_client):
        # real registry.deregister is a safe no-op, so the gateway must check
        # existence (get_agent) to distinguish 404 from idempotent removal.
        resp = gw_client.post(f"{BASE}/ghost/deregister")
        assert resp.status_code == 404
