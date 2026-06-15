"""Unified Dispatcher — routes DispatchRequests across backends with scheduling policies.

Combines: candidate resolution across backends (R2.3), retry + failover (R2.4),
health pre-check (R2.5), per-dispatch timeout + global backpressure (R2.6), and
per-target circuit breaking (R2.7).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional, Protocol

from src.dispatcher.base import DispatchAttempt, DispatchRequest, DispatchResult, DispatchTarget
from src.dispatcher.result_cache import ResultCache
from src.observability.metrics import Metrics, get_metrics
from src.resilience.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState

logger = logging.getLogger(__name__)


@dataclass
class DispatcherConfig:
    """Tunables for the unified dispatcher."""

    max_retries: int = 2  # additional attempts after the first candidate
    dispatch_timeout: float = 300.0  # per-attempt timeout (seconds)
    max_concurrent: int = 8  # global in-flight dispatch cap (backpressure)
    health_precheck: bool = True  # probe candidates before invoking


class _Backend(Protocol):
    async def candidates(
        self, agent_type: str, agent_id: Optional[str] = None
    ) -> list[DispatchTarget]: ...

    async def invoke(self, target: DispatchTarget, request: DispatchRequest) -> DispatchAttempt: ...

    async def health_check(self, target: DispatchTarget) -> bool: ...


class Dispatcher:
    """Routes a DispatchRequest to the first healthy candidate, failing over on error."""

    def __init__(
        self,
        backends: list[_Backend],
        config: Optional[DispatcherConfig] = None,
        result_cache: Optional[ResultCache] = None,
        metrics: Optional[Metrics] = None,
    ) -> None:
        self._backends = backends
        self._config = config or DispatcherConfig()
        self._result_cache = result_cache
        self._metrics = metrics or get_metrics()
        self._semaphore = asyncio.Semaphore(self._config.max_concurrent)
        # per-target circuit breakers, keyed by (kind, agent_id|agent_type)
        self._breakers: dict[tuple[str, str], CircuitBreaker] = {}

    # ── candidate resolution (R2.3) ────────────────────────────────────────────

    async def _resolve(
        self, request: DispatchRequest
    ) -> list[tuple[DispatchTarget, _Backend]]:
        pairs: list[tuple[DispatchTarget, _Backend]] = []
        for backend in self._backends:
            try:
                cands = await backend.candidates(request.agent_type, request.agent_id)
            except Exception:
                logger.warning("candidates() failed for a backend", exc_info=True)
                continue
            for c in cands or []:
                pairs.append((c, backend))
        return pairs

    async def _filter_healthy(
        self, pairs: list[tuple[DispatchTarget, _Backend]]
    ) -> list[tuple[DispatchTarget, _Backend]]:
        healthy: list[tuple[DispatchTarget, _Backend]] = []
        for target, backend in pairs:
            breaker = self._get_breaker(target)
            if breaker.state == CircuitState.OPEN:
                logger.info("Skipping target (circuit open): %s", target)
                continue
            try:
                ok = await backend.health_check(target)
            except Exception:
                ok = False
            if ok:
                healthy.append((target, backend))
        return healthy

    # ── public entrypoint ──────────────────────────────────────────────────────

    async def dispatch(self, request: DispatchRequest) -> DispatchResult:
        """Public entry: times the dispatch and records metrics."""
        start = time.monotonic()
        result = await self._do_dispatch(request)
        if self._metrics:
            self._metrics.record_dispatch(result.success, (time.monotonic() - start) * 1000, request.agent_type)
        return result

    async def _do_dispatch(self, request: DispatchRequest) -> DispatchResult:
        pairs = await self._resolve(request)
        if self._config.health_precheck:
            pairs = await self._filter_healthy(pairs)

        if not pairs:
            cached = self._cached(request)
            if cached is not None:
                return cached
            return DispatchResult(
                success=False,
                error=f"No candidates for agent_type '{request.agent_type}'",
            )

        max_attempts = min(len(pairs), self._config.max_retries + 1)
        attempts: list[DispatchAttempt] = []
        for target, backend in pairs[:max_attempts]:
            attempt = await self._try_one(target, backend, request)
            attempts.append(attempt)
            if attempt.success:
                result = DispatchResult(
                    success=True,
                    output=attempt.output,
                    target=target,
                    attempts=attempts,
                )
                if self._result_cache is not None:
                    self._result_cache.put(request.agent_type, request.task, result)
                return result

        # all candidates failed — try graceful degradation via the result cache
        cached = self._cached(request)
        if cached is not None:
            logger.info("Serving cached result for %s (degraded)", request.agent_type)
            return DispatchResult(
                success=True,
                degraded=True,
                output=cached.output,
                artifacts=cached.artifacts,
                target=cached.target,
                attempts=attempts,
            )

        last = attempts[-1]
        return DispatchResult(
            success=False,
            output=last.output,
            error=last.error or "All candidates failed",
            attempts=attempts,
        )

    def _cached(self, request: DispatchRequest) -> Optional[DispatchResult]:
        """Return a degraded-result wrapper if the cache has a hit, else None."""
        if self._result_cache is None:
            return None
        cached = self._result_cache.get(request.agent_type, request.task)
        if cached is None:
            return None
        return DispatchResult(
            success=True,
            degraded=True,
            output=cached.output,
            artifacts=cached.artifacts,
            target=cached.target,
        )

    # ── single-candidate attempt (R2.6 timeout + backpressure, R2.7 breaker) ──

    async def _try_one(
        self,
        target: DispatchTarget,
        backend: _Backend,
        request: DispatchRequest,
    ) -> DispatchAttempt:
        breaker = self._get_breaker(target)
        if breaker.state == CircuitState.OPEN:
            return DispatchAttempt(target=target, success=False, error="Circuit open")

        timeout = (
            request.timeout if request.timeout is not None else self._config.dispatch_timeout
        )
        async with self._semaphore:  # global backpressure
            try:
                attempt = await asyncio.wait_for(
                    backend.invoke(target, request), timeout=timeout
                )
            except asyncio.TimeoutError:
                breaker.record_failure()
                return DispatchAttempt(
                    target=target, success=False, error=f"Dispatch timed out after {timeout}s"
                )
            except CircuitOpenError:  # defensive (backends don't raise this today)
                return DispatchAttempt(target=target, success=False, error="Circuit open")
            except Exception as e:
                breaker.record_failure()
                logger.warning("Dispatch invoke error for %s: %s", target, e)
                return DispatchAttempt(
                    target=target, success=False, error=f"invoke error: {e!s}"
                )

        # backend returned an attempt — record success/failure on the breaker
        if attempt.success:
            breaker.record_success()
        else:
            breaker.record_failure()
        return attempt

    # ── breaker bookkeeping ───────────────────────────────────────────────────

    def _breaker_key(self, target: DispatchTarget) -> tuple[str, str]:
        return (target.kind, target.agent_id or target.agent_type)

    def _get_breaker(self, target: DispatchTarget) -> CircuitBreaker:
        key = self._breaker_key(target)
        breaker = self._breakers.get(key)
        if breaker is None:
            breaker = CircuitBreaker()
            self._breakers[key] = breaker
        return breaker
