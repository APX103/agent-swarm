"""Wave 5 tests: resilient periodic registry health-sweep loop.

AgentRegistry.health_sweep() prunes orphaned skill-index entries but is never
called in production. This loop drives it periodically, surviving per-iteration
errors and stopping cleanly on shutdown.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.registry.sweeper import health_sweep_loop


@pytest.mark.asyncio
async def test_sweep_loop_calls_health_sweep():
    reg = MagicMock()
    reg.health_sweep = AsyncMock(return_value=0)
    task = asyncio.create_task(health_sweep_loop(reg, interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert reg.health_sweep.await_count >= 1


@pytest.mark.asyncio
async def test_sweep_loop_continues_after_error():
    reg = MagicMock()
    n = {"i": 0}

    async def sweep():
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("redis blip")
        return 0

    reg.health_sweep = sweep
    task = asyncio.create_task(health_sweep_loop(reg, interval=0.01))
    await asyncio.sleep(0.06)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert n["i"] >= 2  # the loop survived the first failure


@pytest.mark.asyncio
async def test_sweep_loop_stops_on_event():
    reg = MagicMock()
    reg.health_sweep = AsyncMock(return_value=0)
    stop = asyncio.Event()
    task = asyncio.create_task(health_sweep_loop(reg, interval=0.01, stop_event=stop))
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)  # exits promptly once stopped
    assert reg.health_sweep.await_count >= 1
