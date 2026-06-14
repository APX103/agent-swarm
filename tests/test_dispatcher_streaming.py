"""W14 tests: DockerBackend streams worker progress via an on_progress callback.

When a DispatchRequest carries an on_progress callable, DockerBackend sends
non-blocking and polls the worker, forwarding each snapshot. Without one, the
blocking path is unchanged.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.common.a2a_client import A2ATask
from src.container_pool.pool import PooledContainer
from src.dispatcher.base import DispatchRequest, DispatchTarget
from src.dispatcher.backends import DockerBackend


def _container() -> PooledContainer:
    return PooledContainer(container_id="c1", container_name="n", port=9001)


@pytest.mark.asyncio
async def test_docker_backend_streams_progress_when_callback_set(monkeypatch):
    pool = MagicMock()
    pool.checkout = AsyncMock(return_value=_container())
    pool.return_container = AsyncMock()

    progress: list[dict] = []

    async def on_progress(event: dict) -> None:
        progress.append(event)

    init = A2ATask(task_id="t1", state="working", message="")
    snap1 = A2ATask(task_id="t1", state="working", message="step1")
    snap2 = A2ATask(task_id="t1", state="completed", message="DONE")

    class FakeClient:
        def __init__(self, base_url, timeout=30.0):
            pass

        async def send_message(self, msg, blocking=True):
            return init  # non-blocking handshake

        async def poll_task(self, task_id, interval=2.0, timeout=300.0):
            yield snap1
            yield snap2

        async def close(self):
            pass

    monkeypatch.setattr("src.dispatcher.backends.A2AClient", FakeClient)

    backend = DockerBackend(pool=pool, model="m", base_url="u", api_key="k")
    target = DispatchTarget(kind="docker", agent_type="frontend-ux-pro")
    req = DispatchRequest(agent_type="frontend-ux-pro", task="do", on_progress=on_progress)

    attempt = await backend.invoke(target, req)

    assert attempt.success is True
    assert attempt.output == "DONE"
    assert len(progress) == 2  # both snapshots forwarded
    assert progress[0]["message"] == "step1"
    assert progress[1]["state"] == "completed"
    pool.return_container.assert_awaited_once_with("c1")


@pytest.mark.asyncio
async def test_docker_backend_blocking_when_no_callback(monkeypatch):
    """Without on_progress the blocking path is used (unchanged behavior)."""
    pool = MagicMock()
    pool.checkout = AsyncMock(return_value=_container())
    pool.return_container = AsyncMock()

    final = A2ATask(task_id="t1", state="completed", message="done")

    sent_blocking: list[bool] = []

    class FakeClient:
        def __init__(self, base_url, timeout=30.0):
            pass

        async def send_message(self, msg, blocking=True):
            sent_blocking.append(blocking)
            return final

        async def close(self):
            pass

    monkeypatch.setattr("src.dispatcher.backends.A2AClient", FakeClient)

    backend = DockerBackend(pool=pool, model="m", base_url="u", api_key="k")
    req = DispatchRequest(agent_type="x", task="do")  # no on_progress
    attempt = await backend.invoke(DispatchTarget(kind="docker", agent_type="x"), req)

    assert attempt.success is True
    assert sent_blocking == [True]  # blocking path
