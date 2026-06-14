"""Wave 8 tests: real orchestration cancellation.

The WS cancel action previously only flipped status; the background orchestration
kept running. register_running/cancel_running track and cancel the live task.
"""
import asyncio

import pytest

from src.api.routes import _running_orchestrations, cancel_running, register_running


def setup_function():
    _running_orchestrations.clear()


@pytest.mark.asyncio
async def test_cancel_running_task():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def long_running():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    t = asyncio.create_task(long_running())
    register_running("task-1", t)
    await started.wait()

    assert cancel_running("task-1") is True
    with pytest.raises(asyncio.CancelledError):
        await t
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_cancel_unknown_task_returns_false():
    assert cancel_running("does-not-exist") is False


@pytest.mark.asyncio
async def test_cancel_completed_task_returns_false():
    async def quick():
        return "done"

    t = asyncio.create_task(quick())
    register_running("task-2", t)
    await t  # let it finish
    assert cancel_running("task-2") is False  # already done, nothing to cancel


@pytest.mark.asyncio
async def test_done_task_is_unregistered():
    async def quick():
        return "done"

    t = asyncio.create_task(quick())
    register_running("task-3", t)
    await t
    # done_callback fires on the loop; give it a tick
    await asyncio.sleep(0)
    assert "task-3" not in _running_orchestrations
