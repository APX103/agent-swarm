"""Wave 7 tests: per-tenant concurrency cap (backpressure at /api/chat).

Prevents one tenant from saturating the worker pool with in-flight orchestrations.
"""
import asyncio

import pytest

from src.api.routes import _get_tenant_semaphore, _tenant_semaphores


def setup_function():
    _tenant_semaphores.clear()


@pytest.mark.asyncio
async def test_same_tenant_gets_same_semaphore():
    s1 = await _get_tenant_semaphore("tenantA", limit=2)
    s2 = await _get_tenant_semaphore("tenantA", limit=2)
    assert s1 is s2


@pytest.mark.asyncio
async def test_different_tenants_get_distinct_semaphores():
    sA = await _get_tenant_semaphore("tenantA", limit=2)
    sB = await _get_tenant_semaphore("tenantB", limit=2)
    assert sA is not sB


@pytest.mark.asyncio
async def test_semaphore_serializes_concurrent_sections():
    sem = await _get_tenant_semaphore("tenantC", limit=1)
    state = {"n": 0, "peak": 0}

    async def section():
        async with sem:
            state["n"] += 1
            state["peak"] = max(state["peak"], state["n"])
            await asyncio.sleep(0.02)
            state["n"] -= 1

    await asyncio.gather(section(), section(), section())
    assert state["peak"] == 1  # limit=1 -> never more than one in flight
