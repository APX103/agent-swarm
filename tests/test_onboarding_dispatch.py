"""W5: onboarding→dispatch regression + selection reliability.

The team's core need ("register my agent → it actually gets mobilized"): a registered
external agent must become a dispatch candidate (by skill) and get invoked; an unknown
agent_type must fail with a clear error, not silently.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.adapter_manager import AdapterManager
from src.adapters.base import AgentResult
from src.dispatcher.backends import ExternalAgentBackend
from src.dispatcher.base import DispatchRequest
from src.dispatcher.dispatcher import Dispatcher, DispatcherConfig
from src.registry.registry import AgentRegistry
from tests.test_registry import FakeRedis


def _registry_with_store() -> AgentRegistry:
    reg = AgentRegistry(redis_url="redis://x", heartbeat_ttl=30, heartbeat_interval=10)
    reg._redis = FakeRedis({})
    return reg


@pytest.mark.asyncio
async def test_registered_external_agent_is_mobilized():
    """Register an external agent (skill) → Dispatcher selects + invokes it."""
    registry = _registry_with_store()
    agent_id = await registry.register({
        "name": "data-bot", "endpoint": "http://data:8000",
        "protocol": "openai", "skills": ["data-analysis"],
    })

    am = AdapterManager()
    adapter = MagicMock()
    adapter.invoke = AsyncMock(return_value=AgentResult(success=True, output="data result"))
    adapter.health_check = AsyncMock(return_value=True)
    am.register(agent_id, adapter)

    dispatcher = Dispatcher(
        [ExternalAgentBackend(registry=registry, adapter_manager=am)],
        DispatcherConfig(health_precheck=True),
    )
    result = await dispatcher.dispatch(DispatchRequest(agent_type="data-analysis", task="analyze"))

    assert result.success is True
    assert result.output == "data result"
    assert result.target is not None and result.target.kind == "external"


@pytest.mark.asyncio
async def test_unknown_agent_type_fails_clearly():
    """No backend serves the type → explicit failure naming the type."""
    registry = _registry_with_store()  # empty
    dispatcher = Dispatcher(
        [ExternalAgentBackend(registry=registry, adapter_manager=AdapterManager())],
        DispatcherConfig(health_precheck=False),
    )
    result = await dispatcher.dispatch(DispatchRequest(agent_type="nonexistent-role", task="x"))
    assert result.success is False
    assert "nonexistent-role" in (result.error or "")
