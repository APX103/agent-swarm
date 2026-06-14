"""W1: verify the swarm_sdk self-registration client (the recommended chatbot→Worker path).

The SDK posts to the gateway; mock the gateway HTTP and assert register/heartbeat/deregister
behave. This was previously untested ("dead code").
"""
import httpx
import pytest
import respx

from src.swarm_sdk.client import AgentClient

GW = "http://orch.test"


@pytest.mark.asyncio
async def test_register_returns_agent_id(respx_mock):
    respx.post(f"{GW}/api/v1/agents/register").mock(
        return_value=httpx.Response(200, json={"agent_id": "a1", "status": "registered"})
    )
    c = AgentClient(GW)
    aid = await c.register(
        name="my-chatbot", endpoint="http://my-bot:8000", protocol="openai", skills=["summarize"]
    )
    assert aid == "a1"
    assert c._agent_id == "a1"


@pytest.mark.asyncio
async def test_heartbeat_true_on_ok(respx_mock):
    respx.post(f"{GW}/api/v1/agents/a1/heartbeat").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    c = AgentClient(GW)
    assert await c.heartbeat("a1") is True


@pytest.mark.asyncio
async def test_heartbeat_false_on_error(respx_mock):
    respx.post(f"{GW}/api/v1/agents/a1/heartbeat").mock(return_value=httpx.Response(500))
    c = AgentClient(GW)
    assert await c.heartbeat("a1") is False


@pytest.mark.asyncio
async def test_deregister_clears_agent_id(respx_mock):
    respx.post(f"{GW}/api/v1/agents/a1/deregister").mock(
        return_value=httpx.Response(200, json={"status": "deregistered"})
    )
    c = AgentClient(GW)
    c._agent_id = "a1"
    await c.deregister()
    assert c._agent_id is None


@pytest.mark.asyncio
async def test_context_manager_deregisters_on_exit(respx_mock):
    respx.post(f"{GW}/api/v1/agents/register").mock(
        return_value=httpx.Response(200, json={"agent_id": "a2", "status": "registered"})
    )
    respx.post(f"{GW}/api/v1/agents/a2/deregister").mock(
        return_value=httpx.Response(200, json={"status": "deregistered"})
    )
    async with AgentClient(GW) as c:
        await c.register(name="bot", endpoint="http://b", protocol="openai")
        assert c._agent_id == "a2"
    # exited → deregistered
    assert c._agent_id is None
