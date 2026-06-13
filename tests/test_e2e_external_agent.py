"""E2E integration tests: External Agent full lifecycle.
Uses InMemoryRegistry (no Redis) + httpx respx mock for external agent + TestClient.
"""
import asyncio
import time
import uuid

import pytest
import respx
from fastapi import FastAPI
from starlette.testclient import TestClient
import httpx

from src.gateway.routes import router, set_deps
from src.adapters.adapter_manager import AdapterManager


# ── In-memory Registry ────────────────────────────────────────────────────────

class InMemoryRegistry:
    def __init__(self, ttl=30):
        self._agents: dict[str, dict] = {}
        self._ttl = ttl
        self._events: list[dict] = []

    async def connect(self): pass

    async def register(self, name, endpoint, protocol="http", skills=None,
                       capabilities=None, version="1.0", heartbeat_interval=10, **extra):
        agent_id = str(uuid.uuid4())[:8]
        now = time.time()
        self._agents[agent_id] = {
            "id": agent_id, "name": name, "endpoint": endpoint,
            "protocol": protocol, "skills": skills or [],
            "capabilities": capabilities or {}, "version": version,
            "heartbeat_interval": heartbeat_interval,
            "registered_at": now, "last_heartbeat": now,
        }
        self._events.append({"type": "online", "agent_id": agent_id, "name": name})
        return agent_id

    async def heartbeat(self, agent_id):
        if agent_id not in self._agents:
            raise KeyError(f"Agent {agent_id} not found")
        self._agents[agent_id]["last_heartbeat"] = time.time()
        return self._agents[agent_id]["heartbeat_interval"]

    async def deregister(self, agent_id):
        if agent_id not in self._agents:
            raise KeyError(f"Agent {agent_id} not found")
        info = self._agents.pop(agent_id)
        self._events.append({"type": "offline", "agent_id": agent_id, "name": info["name"]})

    async def get_agent(self, agent_id):
        return self._agents.get(agent_id)

    async def list_agents(self):
        return list(self._agents.values())

    async def find_by_skill(self, skill):
        return [a for a in self._agents.values() if skill in a.get("skills", [])]

    async def close(self):
        self._agents.clear()

    def get_events(self):
        return list(self._events)

    def sync_register(self, name, endpoint, **kw):
        return asyncio.get_event_loop().run_until_complete(
            self.register(name=name, endpoint=endpoint, **kw))

    def sync_deregister(self, agent_id):
        return asyncio.get_event_loop().run_until_complete(self.deregister(agent_id))

    def sync_heartbeat(self, agent_id):
        return asyncio.get_event_loop().run_until_complete(self.heartbeat(agent_id))


# ── Fixtures ──────────────────────────────────────────────────────────────────

AGENT_URL = "http://fake-agent.test"


@pytest.fixture
def e2e_setup(respx_mock):
    """Set up InMemoryRegistry + AdapterManager + TestClient + mocked external agent."""
    respx.get(f"{AGENT_URL}/v1/models").respond(json={"data": [{"id": "fake-model"}]})
    respx.get(f"{AGENT_URL}/health").respond(json={"status": "ok"})
    respx.post(f"{AGENT_URL}/v1/chat/completions").mock(
        side_effect=lambda req: httpx.Response(200, json={
            "id": "chatcmpl-fake",
            "choices": [{
                "message": {"role": "assistant", "content": "Agent completed: echo-task"},
                "finish_reason": "stop",
            }],
        }),
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    registry = InMemoryRegistry(ttl=30)
    adapter_mgr = AdapterManager()

    app = FastAPI()
    app.include_router(router)
    set_deps(registry, adapter_mgr)

    client = TestClient(app, raise_server_exceptions=False)

    yield {
        "client": client, "registry": registry, "adapter_mgr": adapter_mgr,
        "base": "/api/v1/agents", "agent_endpoint": AGENT_URL,
    }

    loop.run_until_complete(registry.close())
    loop.close()


# ═══ Phase 1: Registration ═══

class TestE2ERegistration:
    def test_register_openai_agent(self, e2e_setup):
        r = e2e_setup["client"].post(f'{e2e_setup["base"]}/register', json={
            "name": "friend-code-agent", "endpoint": e2e_setup["agent_endpoint"],
            "protocol": "openai", "skills": ["python", "code-review"],
        })
        assert r.status_code == 200
        assert "agent_id" in r.json() and r.json()["status"] == "registered"

    def test_register_cli_agent(self, e2e_setup):
        r = e2e_setup["client"].post(f'{e2e_setup["base"]}/register', json={
            "name": "friend-cli", "endpoint": "python3", "protocol": "cli", "args": ["-c"],
        })
        assert r.status_code == 200

    def test_register_mcp_agent(self, e2e_setup):
        r = e2e_setup["client"].post(f'{e2e_setup["base"]}/register', json={
            "name": "friend-mcp", "endpoint": e2e_setup["agent_endpoint"], "protocol": "mcp",
        })
        assert r.status_code == 200

    def test_register_missing_name(self, e2e_setup):
        r = e2e_setup["client"].post(f'{e2e_setup["base"]}/register', json={"endpoint": "http://x"})
        assert r.status_code == 422

    def test_register_missing_endpoint(self, e2e_setup):
        r = e2e_setup["client"].post(f'{e2e_setup["base"]}/register', json={"name": "x"})
        assert r.status_code == 422

    def test_register_creates_online_events(self, e2e_setup):
        for n in ("e1", "e2", "e3"):
            e2e_setup["registry"].sync_register(n, "http://x")
        assert len([e for e in e2e_setup["registry"].get_events() if e["type"] == "online"]) == 3


# ═══ Phase 2: Heartbeat ═══

class TestE2EHeartbeat:
    def test_heartbeat_success(self, e2e_setup):
        r = e2e_setup["client"].post(f'{e2e_setup["base"]}/register', json={
            "name": "hb", "endpoint": e2e_setup["agent_endpoint"],
        })
        aid = r.json()["agent_id"]
        r = e2e_setup["client"].post(f'{e2e_setup["base"]}/{aid}/heartbeat')
        assert r.status_code == 200 and r.json()["status"] == "ok"

    def test_heartbeat_updates_timestamp(self, e2e_setup):
        aid = e2e_setup["registry"].sync_register("ts", "http://x")
        before = e2e_setup["registry"]._agents[aid]["last_heartbeat"]
        time.sleep(0.05)
        e2e_setup["registry"].sync_heartbeat(aid)
        assert e2e_setup["registry"]._agents[aid]["last_heartbeat"] > before

    def test_heartbeat_unknown_agent(self, e2e_setup):
        r = e2e_setup["client"].post(f'{e2e_setup["base"]}/nope/heartbeat')
        assert r.status_code == 404


# ═══ Phase 3: Listing & Query ═══

class TestE2EQuery:
    def test_list_all_agents(self, e2e_setup):
        for n in ("a1", "a2", "a3"):
            e2e_setup["registry"].sync_register(n, "http://x")
        assert len(e2e_setup["client"].get(e2e_setup["base"]).json()) == 3

    def test_find_by_skill(self, e2e_setup):
        e2e_setup["registry"].sync_register("code", "http://x", skills=["python"])
        e2e_setup["registry"].sync_register("data", "http://x", skills=["analysis"])
        agents = e2e_setup["client"].get(f'{e2e_setup["base"]}?skill=python').json()
        assert len(agents) == 1 and agents[0]["name"] == "code"

    def test_find_by_skill_no_match(self, e2e_setup):
        assert len(e2e_setup["client"].get(f'{e2e_setup["base"]}?skill=nope').json()) == 0

    def test_agent_has_complete_fields(self, e2e_setup):
        e2e_setup["registry"].sync_register("full", "http://x")
        agent = e2e_setup["client"].get(e2e_setup["base"]).json()[0]
        for key in ("id", "name", "endpoint", "protocol", "skills"):
            assert key in agent


# ═══ Phase 4: Invocation ═══

class TestE2EInvocation:
    def test_invoke_openai_agent(self, e2e_setup):
        aid = e2e_setup["registry"].sync_register("inv", e2e_setup["agent_endpoint"])
        e2e_setup["adapter_mgr"].register_from_info(aid, {
            "protocol": "openai", "base_url": e2e_setup["agent_endpoint"], "model": "fake",
        })
        r = e2e_setup["client"].post(f'{e2e_setup["base"]}/{aid}/invoke', json={"task": "quicksort"})
        assert r.status_code == 200 and r.json()["success"] is True

    def test_invoke_missing_task(self, e2e_setup):
        r = e2e_setup["client"].post(f'{e2e_setup["base"]}/any/invoke', json={"context": {}})
        assert r.status_code == 422

    def test_invoke_agent_not_found(self, e2e_setup):
        r = e2e_setup["client"].post(f'{e2e_setup["base"]}/nope/invoke', json={"task": "x"})
        assert r.status_code == 404

    def test_invoke_returns_result(self, e2e_setup):
        aid = e2e_setup["registry"].sync_register("echo", e2e_setup["agent_endpoint"])
        e2e_setup["adapter_mgr"].register_from_info(aid, {
            "protocol": "openai", "base_url": e2e_setup["agent_endpoint"], "model": "fake",
        })
        r = e2e_setup["client"].post(f'{e2e_setup["base"]}/{aid}/invoke', json={"task": "test"})
        assert r.status_code == 200 and r.json()["success"] is True


# ═══ Phase 5: Deregistration ═══

class TestE2EDeregistration:
    def test_deregister_success(self, e2e_setup):
        aid = e2e_setup["registry"].sync_register("del", "http://x")
        r = e2e_setup["client"].post(f'{e2e_setup["base"]}/{aid}/deregister')
        assert r.status_code == 200

    def test_deregistered_heartbeat_404(self, e2e_setup):
        aid = e2e_setup["registry"].sync_register("hb-del", "http://x")
        e2e_setup["registry"].sync_deregister(aid)
        assert e2e_setup["client"].post(f'{e2e_setup["base"]}/{aid}/heartbeat').status_code == 404

    def test_deregistered_not_in_list(self, e2e_setup):
        e2e_setup["registry"].sync_register("l1", "http://x")
        aid = e2e_setup["registry"].sync_register("l2", "http://x")
        e2e_setup["registry"].sync_deregister(aid)
        assert len(e2e_setup["client"].get(e2e_setup["base"]).json()) == 1

    def test_double_deregister_404(self, e2e_setup):
        aid = e2e_setup["registry"].sync_register("dbl", "http://x")
        e2e_setup["registry"].sync_deregister(aid)
        assert e2e_setup["client"].post(f'{e2e_setup["base"]}/{aid}/deregister').status_code == 404

    def test_offline_event(self, e2e_setup):
        aid = e2e_setup["registry"].sync_register("ev", "http://x")
        e2e_setup["registry"].sync_deregister(aid)
        offline = [e for e in e2e_setup["registry"].get_events() if e["type"] == "offline"]
        assert len(offline) == 1 and offline[0]["agent_id"] == aid


# ═══ Phase 6: TTL Expiration ═══

class TestE2ETTL:
    def test_expired_agent_removed(self, e2e_setup):
        aid = e2e_setup["registry"].sync_register("exp", "http://x")
        e2e_setup["registry"]._agents[aid]["last_heartbeat"] = time.time() - 100
        expired = [k for k, v in e2e_setup["registry"]._agents.items()
                   if time.time() - v["last_heartbeat"] > e2e_setup["registry"]._ttl]
        for k in expired:
            e2e_setup["registry"]._agents.pop(k)
        assert aid not in [a["id"] for a in e2e_setup["client"].get(e2e_setup["base"]).json()]

    def test_expired_agent_heartbeat_404(self, e2e_setup):
        aid = e2e_setup["registry"].sync_register("exp-hb", "http://x")
        e2e_setup["registry"]._agents[aid]["last_heartbeat"] = time.time() - 100
        expired = [k for k, v in e2e_setup["registry"]._agents.items()
                   if time.time() - v["last_heartbeat"] > e2e_setup["registry"]._ttl]
        for k in expired:
            e2e_setup["registry"]._agents.pop(k)
        assert e2e_setup["client"].post(f'{e2e_setup["base"]}/{aid}/heartbeat').status_code == 404


# ═══ Phase 7: Re-registration ═══

class TestE2EReregistration:
    def test_reregister_new_agent(self, e2e_setup):
        r = e2e_setup["client"].post(f'{e2e_setup["base"]}/register', json={
            "name": "new", "endpoint": e2e_setup["agent_endpoint"], "protocol": "openai",
        })
        assert r.status_code == 200

    def test_reregister_skill_query(self, e2e_setup):
        e2e_setup["registry"].sync_register("rust", "http://x", skills=["rust"])
        agents = e2e_setup["client"].get(f'{e2e_setup["base"]}?skill=rust').json()
        assert len(agents) == 1 and agents[0]["name"] == "rust"

    def test_consecutive_heartbeats(self, e2e_setup):
        aid = e2e_setup["registry"].sync_register("hb-loop", "http://x")
        for _ in range(3):
            assert e2e_setup["client"].post(f'{e2e_setup["base"]}/{aid}/heartbeat').status_code == 200
