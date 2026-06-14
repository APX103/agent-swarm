"""R1.2 tests: gateway register auto-provisions an adapter (single-step onboarding).

After R1.2, POST /api/v1/agents/register with an adapter protocol (openai/cli/mcp/a2a)
must build & register the adapter immediately, so POST /{id}/invoke works without a
separate manual ``register_from_info`` step. Unknown protocols are rejected (400) with
nothing persisted; plain ``http`` is register-only (no adapter, invoke → 404).
"""
import httpx
import pytest
import respx
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.gateway.routes import router, set_deps
from src.registry.registry import AgentRegistry
from src.adapters.adapter_manager import AdapterManager
from tests.test_registry import FakeRedis

BASE = "/api/v1/agents"
AGENT_URL = "http://fake-openai.test"


@pytest.fixture
def gw_with_adapters(respx_mock):
    # Mock the OpenAI-compatible chat endpoint so an immediate invoke succeeds.
    respx.post(f"{AGENT_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        }),
    )

    registry = AgentRegistry(
        redis_url="redis://localhost:6379/0", heartbeat_ttl=30, heartbeat_interval=10,
    )
    registry._redis = FakeRedis({})
    adapter_manager = AdapterManager()

    app = FastAPI()
    app.include_router(router)
    set_deps(registry, adapter_manager)
    client = TestClient(app, raise_server_exceptions=False)
    yield client, adapter_manager


class TestGatewayAutoProvision:
    def test_register_openai_then_invoke_no_manual_step(self, gw_with_adapters):
        client, _ = gw_with_adapters
        r = client.post(f"{BASE}/register", json={
            "name": "auto-openai", "endpoint": AGENT_URL, "protocol": "openai",
        })
        assert r.status_code == 200, r.text
        aid = r.json()["agent_id"]

        # invoke immediately — adapter must have been auto-provisioned on register
        r2 = client.post(f"{BASE}/{aid}/invoke", json={"task": "hello"})
        assert r2.status_code == 200, r2.text
        assert r2.json()["success"] is True

    def test_register_unknown_protocol_400_and_not_persisted(self, gw_with_adapters):
        client, _ = gw_with_adapters
        r = client.post(f"{BASE}/register", json={
            "name": "bad", "endpoint": "http://x", "protocol": "foobar",
        })
        assert r.status_code == 400
        # nothing persisted on rejected registration
        assert client.get(BASE).json() == []

    def test_register_http_protocol_is_register_only(self, gw_with_adapters):
        client, _ = gw_with_adapters
        r = client.post(f"{BASE}/register", json={
            "name": "generic", "endpoint": "http://x", "protocol": "http",
        })
        assert r.status_code == 200
        aid = r.json()["agent_id"]
        # plain http has no adapter → invoke 404
        assert client.post(f"{BASE}/{aid}/invoke", json={"task": "x"}).status_code == 404

    def test_register_mcp_auto_provisions(self, gw_with_adapters):
        client, adapter_manager = gw_with_adapters
        r = client.post(f"{BASE}/register", json={
            "name": "mcp-agent", "endpoint": AGENT_URL, "protocol": "mcp",
        })
        assert r.status_code == 200
        aid = r.json()["agent_id"]
        # mcp auto-provisioned an adapter from endpoint -> server_url
        assert aid in adapter_manager.list_agents()
