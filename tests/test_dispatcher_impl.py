"""R2.3–R2.7 tests: the Dispatcher (unified candidate resolution + scheduling).

Covers: candidate resolution across backends (R2.3), retry + failover (R2.4),
health pre-check (R2.5), per-dispatch timeout + global backpressure (R2.6), and
per-target circuit breaking (R2.7).
"""
import asyncio

import pytest

from src.dispatcher.base import DispatchAttempt, DispatchRequest, DispatchTarget
from src.dispatcher.dispatcher import Dispatcher, DispatcherConfig


# ── fakes ──────────────────────────────────────────────────────────────────────


def _t(kind: str, ident: str) -> DispatchTarget:
    return DispatchTarget(
        kind=kind, agent_type=ident, agent_id=ident if kind == "external" else None
    )


async def _ok(t, r):
    return DispatchAttempt(target=t, success=True, output="ok")


async def _fail(t, r):
    return DispatchAttempt(target=t, success=False, error="boom")


class FakeBackend:
    def __init__(self, targets, invoke_fn, health_fn=None):
        self._targets = targets
        self._invoke_fn = invoke_fn
        self._health_fn = health_fn or (lambda t: True)
        self.invoke_calls = 0

    async def candidates(self, agent_type):
        return list(self._targets)

    async def invoke(self, target, request):
        self.invoke_calls += 1
        return await self._invoke_fn(target, request)

    async def health_check(self, target):
        return self._health_fn(target)


# ── R2.3 candidate resolution ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_candidates_combined_across_backends_with_failover():
    docker_t = _t("docker", "a")
    ext_t = _t("external", "e1")
    b1 = FakeBackend([docker_t], invoke_fn=_fail)
    b2 = FakeBackend([ext_t], invoke_fn=_ok)
    d = Dispatcher([b1, b2], DispatcherConfig(max_retries=2, health_precheck=False))

    result = await d.dispatch(DispatchRequest(agent_type="a", task="x"))
    assert result.success is True
    assert result.target is ext_t  # failed over from docker (b1) to external (b2)


@pytest.mark.asyncio
async def test_no_candidates_returns_failure():
    backend = FakeBackend([], invoke_fn=_ok)
    d = Dispatcher([backend], DispatcherConfig(health_precheck=False))
    result = await d.dispatch(DispatchRequest(agent_type="a", task="x"))
    assert result.success is False
    assert "No candidates" in (result.error or "")


# ── R2.4 retry + failover ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_candidate_fails_second_succeeds():
    fail_t = _t("docker", "a")
    ok_t = _t("external", "e1")

    async def invoke(t, r):
        return await _fail(t, r) if t is fail_t else await _ok(t, r)

    backend = FakeBackend([fail_t, ok_t], invoke_fn=invoke)
    d = Dispatcher([backend], DispatcherConfig(max_retries=2, health_precheck=False))
    result = await d.dispatch(DispatchRequest(agent_type="a", task="x"))
    assert result.success is True
    assert len(result.attempts) == 2
    assert result.target is ok_t


@pytest.mark.asyncio
async def test_max_retries_caps_attempts():
    # three candidates all failing, max_retries=1 -> at most 2 attempts
    targets = [_t("external", f"e{i}") for i in range(3)]
    backend = FakeBackend(targets, invoke_fn=_fail)
    d = Dispatcher([backend], DispatcherConfig(max_retries=1, health_precheck=False))
    result = await d.dispatch(DispatchRequest(agent_type="a", task="x"))
    assert result.success is False
    assert len(result.attempts) == 2


@pytest.mark.asyncio
async def test_all_fail_returns_failure_with_attempts():
    backend = FakeBackend([_t("docker", "a")], invoke_fn=_fail)
    d = Dispatcher([backend], DispatcherConfig(max_retries=0, health_precheck=False))
    result = await d.dispatch(DispatchRequest(agent_type="a", task="x"))
    assert result.success is False
    assert len(result.attempts) == 1


# ── R2.5 health pre-check ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unhealthy_candidate_is_skipped():
    bad = _t("docker", "a")
    good = _t("external", "e1")

    async def invoke(t, r):
        return await _fail(t, r) if t is bad else await _ok(t, r)

    def health(t):
        return False if t is bad else True

    backend = FakeBackend([bad, good], invoke_fn=invoke, health_fn=health)
    d = Dispatcher([backend], DispatcherConfig(max_retries=2, health_precheck=True))
    result = await d.dispatch(DispatchRequest(agent_type="a", task="x"))
    assert result.success is True
    assert result.target is good  # bad skipped by pre-check, never invoked


# ── R2.6 timeout + backpressure ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_slow_candidate_times_out():
    slow = _t("docker", "a")

    async def invoke(t, r):
        await asyncio.sleep(0.3)
        return await _ok(t, r)

    backend = FakeBackend([slow], invoke_fn=invoke)
    d = Dispatcher(
        [backend], DispatcherConfig(max_retries=0, dispatch_timeout=0.1, health_precheck=False)
    )
    result = await d.dispatch(DispatchRequest(agent_type="a", task="x"))
    assert result.success is False
    assert "timed out" in (result.attempts[-1].error or "").lower()


@pytest.mark.asyncio
async def test_backpressure_caps_concurrency():
    # max_concurrent=1: two parallel dispatches to independent targets serialize.
    t1, t2 = _t("external", "e1"), _t("external", "e2")
    state = {"active": 0, "peak": 0}

    async def _invoke_tracked(t, r):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0.05)
        state["active"] -= 1
        return await _ok(t, r)

    backend = FakeBackend([t1], invoke_fn=_invoke_tracked)
    backend2 = FakeBackend([t2], invoke_fn=_invoke_tracked)
    d = Dispatcher(
        [backend, backend2], DispatcherConfig(max_retries=0, max_concurrent=1, health_precheck=False)
    )
    await asyncio.gather(
        d.dispatch(DispatchRequest(agent_type="e1", task="x")),
        d.dispatch(DispatchRequest(agent_type="e2", task="x")),
    )
    assert state["peak"] == 1  # never more than one invoke in flight


# ── R2.7 per-target circuit breaker ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repeated_failure_trips_breaker():
    target = _t("docker", "a")
    backend = FakeBackend([target], invoke_fn=_fail)
    d = Dispatcher([backend], DispatcherConfig(max_retries=0, health_precheck=False))
    req = DispatchRequest(agent_type="a", task="x")

    for _ in range(5):  # failure_threshold default = 5
        r = await d.dispatch(req)
        assert r.success is False

    r = await d.dispatch(req)
    assert r.success is False
    # breaker now OPEN → _try_one short-circuits with a circuit message
    assert "circuit" in (r.attempts[-1].error or "").lower()
