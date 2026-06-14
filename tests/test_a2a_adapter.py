"""R1.4 tests: a real A2A adapter (replaces the OpenAIAdapter alias for protocol 'a2a').

The adapter wraps A2AClient (JSON-RPC message/send over HTTP), maps the returned
A2ATask to AgentResult, and health-checks via /.well-known/agent.json.
"""
import httpx
import pytest
import respx

from src.adapters.adapter_manager import create_adapter
from src.adapters.a2a_adapter import A2AAdapter

BASE_URL = "http://a2a.test"


def _rpc(state: str, text: str = "hello result") -> dict:
    return {
        "jsonrpc": "2.0", "id": 1,
        "result": {
            "id": "task-1",
            "status": {"state": state},
            "history": [
                {"role": "agent", "parts": [{"kind": "text", "text": text}], "messageId": "m1"},
            ],
            "artifacts": [],
        },
    }


@pytest.mark.asyncio
async def test_invoke_completed_returns_success(respx_mock):
    respx.post(url__startswith=BASE_URL).mock(return_value=httpx.Response(200, json=_rpc("completed")))
    adapter = A2AAdapter(BASE_URL, timeout=10)
    try:
        result = await adapter.invoke("do something")
        assert result.success is True
        assert result.output == "hello result"
        assert result.metadata["state"] == "completed"
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_invoke_failed_state_returns_failure(respx_mock):
    respx.post(url__startswith=BASE_URL).mock(return_value=httpx.Response(200, json=_rpc("failed", "boom")))
    adapter = A2AAdapter(BASE_URL, timeout=10)
    try:
        result = await adapter.invoke("x")
        assert result.success is False
        assert result.error is not None
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_health_check_true_when_card_available(respx_mock):
    respx.get(url__startswith=f"{BASE_URL}/.well-known").mock(
        return_value=httpx.Response(200, json={"name": "a2a-agent"}))
    adapter = A2AAdapter(BASE_URL, timeout=10)
    try:
        assert await adapter.health_check() is True
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_health_check_false_when_card_missing(respx_mock):
    respx.get(url__startswith=f"{BASE_URL}/.well-known").mock(return_value=httpx.Response(500))
    adapter = A2AAdapter(BASE_URL, timeout=10)
    try:
        assert await adapter.health_check() is False
    finally:
        await adapter.close()


def test_create_adapter_a2a_returns_a2a_adapter():
    adapter = create_adapter({"protocol": "a2a", "base_url": BASE_URL, "timeout": 60})
    assert isinstance(adapter, A2AAdapter)
    assert adapter.base_url == BASE_URL


def test_a2a_adapter_name_and_capabilities():
    adapter = A2AAdapter(BASE_URL)
    assert adapter.name == f"a2a:{BASE_URL}"
    caps = adapter.capabilities
    assert "a2a" in caps.skills
