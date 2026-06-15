"""W11 tests: result cache (graceful degradation L2) + Dispatcher integration.

Successful dispatch results are cached; when every candidate fails, a cached hit
is returned as a degraded success (explicitly flagged) instead of a hard failure.
"""
import time

import pytest

from src.dispatcher.base import DispatchAttempt, DispatchRequest, DispatchResult, DispatchTarget
from src.dispatcher.dispatcher import Dispatcher, DispatcherConfig
from src.dispatcher.result_cache import ResultCache


# ── ResultCache unit tests ─────────────────────────────────────────────────────


def test_put_then_get():
    c = ResultCache(ttl=100)
    c.put("a", "x", DispatchResult(success=True, output="ok"))
    got = c.get("a", "x")
    assert got is not None and got.output == "ok"


def test_miss_returns_none():
    assert ResultCache().get("a", "x") is None


def test_expired_entry_returns_none():
    c = ResultCache(ttl=0.01)
    c.put("a", "x", DispatchResult(success=True, output="ok"))
    time.sleep(0.05)
    assert c.get("a", "x") is None


def test_failure_results_are_not_cached():
    c = ResultCache()
    c.put("a", "x", DispatchResult(success=False, error="boom"))
    assert c.get("a", "x") is None


# ── Dispatcher degradation integration ────────────────────────────────────────


def _t(kind: str, ident: str) -> DispatchTarget:
    return DispatchTarget(kind=kind, agent_type=ident, agent_id=ident if kind == "external" else None)


class _Backend:
    def __init__(self, targets, invoke_fn):
        self._targets = targets
        self._invoke = invoke_fn

    async def candidates(self, agent_type, agent_id=None):
        return list(self._targets)

    async def invoke(self, target, request):
        return await self._invoke(target, request)

    async def health_check(self, target):
        return True


async def _ok(t, r):
    return DispatchAttempt(target=t, success=True, output="OK")


async def _fail(t, r):
    return DispatchAttempt(target=t, success=False, error="boom")


@pytest.mark.asyncio
async def test_all_fail_with_cache_hit_returns_degraded():
    cache = ResultCache(ttl=1000)
    cache.put("a", "x", DispatchResult(success=True, output="CACHED"))
    d = Dispatcher(
        [_Backend([_t("docker", "a")], _fail)],
        DispatcherConfig(max_retries=0, health_precheck=False),
        result_cache=cache,
    )
    result = await d.dispatch(DispatchRequest(agent_type="a", task="x"))
    assert result.success is True
    assert result.degraded is True
    assert result.output == "CACHED"


@pytest.mark.asyncio
async def test_all_fail_without_cache_returns_failure():
    d = Dispatcher(
        [_Backend([_t("docker", "a")], _fail)],
        DispatcherConfig(max_retries=0, health_precheck=False),
        result_cache=ResultCache(),
    )
    result = await d.dispatch(DispatchRequest(agent_type="a", task="x"))
    assert result.success is False
    assert result.degraded is False


@pytest.mark.asyncio
async def test_success_populates_cache():
    cache = ResultCache()
    d = Dispatcher(
        [_Backend([_t("docker", "a")], _ok)],
        DispatcherConfig(max_retries=0, health_precheck=False),
        result_cache=cache,
    )
    await d.dispatch(DispatchRequest(agent_type="a", task="x"))
    assert cache.get("a", "x") is not None
