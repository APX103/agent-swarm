"""R2.2 tests: DockerBackend and ExternalAgentBackend.

Backends are the concrete executors behind the Dispatcher. DockerBackend wraps the
container pool + A2A client; ExternalAgentBackend wraps the registry + adapter manager.
Each exposes candidates(agent_type) and invoke(target, request) -> DispatchAttempt, and
must release resources (container / client) even on failure.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.adapters.base import AgentResult
from src.container_pool.pool import PooledContainer
from src.dispatcher.base import DispatchRequest, DispatchTarget
from src.dispatcher.backends import DockerBackend, ExternalAgentBackend


def _container(cid: str = "c1", port: int = 9001) -> PooledContainer:
    return PooledContainer(container_id=cid, container_name="worker", port=port)


# ── DockerBackend ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_docker_candidates():
    backend = DockerBackend(pool=MagicMock(), model="m", base_url="u", api_key="k")
    cands = await backend.candidates("frontend-ux-pro")
    assert len(cands) == 1
    assert cands[0].kind == "docker"
    assert cands[0].agent_type == "frontend-ux-pro"


@pytest.mark.asyncio
async def test_docker_invoke_success(monkeypatch):
    pool = MagicMock()
    pool.checkout = AsyncMock(return_value=_container())
    pool.return_container = AsyncMock()

    fake_task = MagicMock(state="completed", message="done", task_id="t1")

    class FakeA2AClient:
        def __init__(self, base_url, timeout=30.0):
            self.closed = False

        async def send_message(self, msg, blocking=True):
            return fake_task

        async def close(self):
            self.closed = True

    monkeypatch.setattr("src.dispatcher.backends.A2AClient", FakeA2AClient)

    backend = DockerBackend(pool=pool, model="m", base_url="u", api_key="k")
    target = DispatchTarget(kind="docker", agent_type="frontend-ux-pro")
    req = DispatchRequest(agent_type="frontend-ux-pro", task="build",
                          context={"task_id": "t", "tenant_id": "ten"})
    attempt = await backend.invoke(target, req)

    assert attempt.success is True
    assert attempt.output == "done"
    pool.return_container.assert_awaited_once_with("c1")


@pytest.mark.asyncio
async def test_docker_invoke_pool_empty():
    pool = MagicMock()
    pool.checkout = AsyncMock(return_value=None)
    pool.return_container = AsyncMock()
    backend = DockerBackend(pool=pool, model="m", base_url="u", api_key="k")
    attempt = await backend.invoke(
        DispatchTarget(kind="docker", agent_type="x"),
        DispatchRequest(agent_type="x", task="t"),
    )
    assert attempt.success is False
    pool.return_container.assert_not_awaited()


@pytest.mark.asyncio
async def test_docker_invoke_a2a_none_still_returns_container(monkeypatch):
    pool = MagicMock()
    pool.checkout = AsyncMock(return_value=_container())
    pool.return_container = AsyncMock()

    class FakeA2AClient:
        def __init__(self, base_url, timeout=30.0): pass

        async def send_message(self, msg, blocking=True):
            return None  # worker returned nothing

        async def close(self): pass

    monkeypatch.setattr("src.dispatcher.backends.A2AClient", FakeA2AClient)
    backend = DockerBackend(pool=pool, model="m", base_url="u", api_key="k")
    attempt = await backend.invoke(
        DispatchTarget(kind="docker", agent_type="x"),
        DispatchRequest(agent_type="x", task="t"),
    )
    assert attempt.success is False
    # resource must be released even on failure
    pool.return_container.assert_awaited_once_with("c1")


# ── ExternalAgentBackend ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_external_candidates():
    registry = MagicMock()
    registry.find_by_skill = AsyncMock(return_value=[
        {"id": "a1", "endpoint": "http://a1:9", "skills": ["frontend-ux-pro"]},
        {"id": "a2", "endpoint": "http://a2:9", "skills": ["frontend-ux-pro"]},
    ])
    backend = ExternalAgentBackend(registry=registry, adapter_manager=MagicMock())
    cands = await backend.candidates("frontend-ux-pro")
    assert len(cands) == 2
    assert all(c.kind == "external" for c in cands)
    assert {c.agent_id for c in cands} == {"a1", "a2"}


@pytest.mark.asyncio
async def test_external_invoke_success():
    adapter = MagicMock()
    adapter.invoke = AsyncMock(return_value=AgentResult(success=True, output="ok"))
    am = MagicMock()
    am.get = MagicMock(return_value=adapter)
    backend = ExternalAgentBackend(registry=MagicMock(), adapter_manager=am)
    attempt = await backend.invoke(
        DispatchTarget(kind="external", agent_type="x", agent_id="a1"),
        DispatchRequest(agent_type="x", task="t"),
    )
    assert attempt.success is True
    assert attempt.output == "ok"


@pytest.mark.asyncio
async def test_external_invoke_no_adapter():
    am = MagicMock()
    am.get = MagicMock(return_value=None)
    backend = ExternalAgentBackend(registry=MagicMock(), adapter_manager=am)
    attempt = await backend.invoke(
        DispatchTarget(kind="external", agent_type="x", agent_id="a1"),
        DispatchRequest(agent_type="x", task="t"),
    )
    assert attempt.success is False


@pytest.mark.asyncio
async def test_external_invoke_adapter_failure():
    adapter = MagicMock()
    adapter.invoke = AsyncMock(return_value=AgentResult(success=False, output="", error="boom"))
    am = MagicMock()
    am.get = MagicMock(return_value=adapter)
    backend = ExternalAgentBackend(registry=MagicMock(), adapter_manager=am)
    attempt = await backend.invoke(
        DispatchTarget(kind="external", agent_type="x", agent_id="a1"),
        DispatchRequest(agent_type="x", task="t"),
    )
    assert attempt.success is False
    assert "boom" in (attempt.error or "")


@pytest.mark.asyncio
async def test_docker_backend_uses_configured_worker_host(monkeypatch):
    """A2: worker_host flows into the A2A client URL (so a container orchestrator
    can reach host-published worker ports via host.docker.internal)."""
    pool = MagicMock()
    pool.checkout = AsyncMock(return_value=_container())
    pool.return_container = AsyncMock()
    captured: dict = {}

    class RecClient:
        def __init__(self, base_url, timeout=30.0):
            captured["url"] = base_url

        async def send_message(self, msg, blocking=True):
            return MagicMock(state="completed", message="ok", task_id="t1")

        async def close(self):
            pass

    monkeypatch.setattr("src.dispatcher.backends.A2AClient", RecClient)
    backend = DockerBackend(
        pool=pool, model="m", base_url="u", api_key="k", worker_host="host.docker.internal"
    )
    await backend.invoke(
        DispatchTarget(kind="docker", agent_type="x"), DispatchRequest(agent_type="x", task="t")
    )
    assert "host.docker.internal" in captured["url"]


def test_docker_backend_default_worker_host_is_localhost():
    backend = DockerBackend(pool=MagicMock(), model="m", base_url="u", api_key="k")
    assert backend._worker_host == "localhost"
