"""Cancel propagation tests: DockerBackend propagates CancelledError to worker."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.container_pool.pool import PooledContainer
from src.dispatcher.base import DispatchRequest, DispatchTarget
from src.dispatcher.backends import DockerBackend, ExternalAgentBackend


def _container() -> PooledContainer:
    return PooledContainer(container_id="c1", container_name="n", port=9001)


@pytest.mark.asyncio
async def test_docker_backend_cancel_propagates_to_worker(monkeypatch):
    """When dispatch is cancelled, cancel_task is sent to the worker."""
    pool = MagicMock()
    pool.checkout = AsyncMock(return_value=_container())
    pool.return_container = AsyncMock()

    captured: dict = {}

    class FakeClient:
        def __init__(self, base_url, timeout=30.0):
            captured["instance"] = self
            self.cancel_called_with = None

        async def send_message(self, msg, blocking=False):
            return MagicMock(task_id="worker-task-1", state="working", message="")

        async def poll_task(self, task_id, interval=2.0, timeout=300.0):
            raise asyncio.CancelledError()
            yield  # never reached; makes this an async generator

        async def cancel_task(self, task_id):
            self.cancel_called_with = task_id
            return True

        async def close(self):
            pass

    monkeypatch.setattr("src.dispatcher.backends.A2AClient", FakeClient)

    async def on_progress(event):
        pass

    backend = DockerBackend(pool=pool, model="m", base_url="u", api_key="k")
    req = DispatchRequest(agent_type="x", task="t", on_progress=on_progress)

    with pytest.raises(asyncio.CancelledError):
        await backend.invoke(DispatchTarget(kind="docker", agent_type="x"), req)

    # cancel_task WAS called with the worker's task_id
    assert captured["instance"].cancel_called_with == "worker-task-1"
    # container was returned (cleanup in finally)
    pool.return_container.assert_awaited_once_with("c1")


@pytest.mark.asyncio
async def test_external_backend_cancel_logs_and_raises(monkeypatch):
    """ExternalAgentBackend catches CancelledError gracefully (sync HTTP, can't mid-call cancel)."""
    adapter = MagicMock()
    adapter.invoke = AsyncMock(side_effect=asyncio.CancelledError())
    am = MagicMock()
    am.get = MagicMock(return_value=adapter)

    backend = ExternalAgentBackend(registry=MagicMock(), adapter_manager=am)

    with pytest.raises(asyncio.CancelledError):
        await backend.invoke(
            DispatchTarget(kind="external", agent_type="x", agent_id="a1"),
            DispatchRequest(agent_type="x", task="t"),
        )
