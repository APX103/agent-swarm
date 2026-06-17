import asyncio
import logging
import time
from enum import Enum
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when the circuit breaker is open and rejects a call."""

    pass


class CircuitBreaker:
    """Async circuit breaker with sliding window, slow-call detection, and automatic recovery.

    States:
        CLOSED  – normal operation, requests pass through.
        OPEN    – failures exceeded threshold, requests are rejected immediately.
        HALF_OPEN – timeout elapsed, a limited number of probes are allowed.

    Transitions:
        CLOSED -> OPEN:   when the failure count (including slow calls) >= failure_threshold
        OPEN -> HALF_OPEN: after `timeout` seconds have elapsed since opening
        HALF_OPEN -> CLOSED: when `success_threshold` consecutive successes occur
        HALF_OPEN -> OPEN:   on any failure while in HALF_OPEN
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        success_threshold: int = 3,
        timeout: int = 30,
        slow_call_threshold: float = 15.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.timeout = timeout
        self.slow_call_threshold = slow_call_threshold

        # Sliding window: list of (timestamp, success: bool)
        self._results: list[tuple[float, bool]] = []
        # Max window size to keep memory bounded
        self._window_size: int = failure_threshold + success_threshold

        self._state = CircuitState.CLOSED
        self._opened_at: float = 0.0
        self._half_open_successes: int = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    async def state(self) -> CircuitState:
        """Return the current circuit state, transitioning out of OPEN if timeout elapsed."""
        async with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._opened_at >= self.timeout:
                    self._transition_to(CircuitState.HALF_OPEN)
            return self._state

    async def allow(self) -> bool:
        """Return True if the circuit is not currently OPEN."""
        return (await self.state()) != CircuitState.OPEN

    async def record_success(self) -> None:
        async with self._lock:
            self._push(True)
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_successes += 1
                if self._half_open_successes >= self.success_threshold:
                    logger.info("Circuit breaker transitioning CLOSED (half-open successes reached)")
                    self._transition_to(CircuitState.CLOSED)

    async def record_failure(self) -> None:
        async with self._lock:
            self._push(False)
            if self._state == CircuitState.HALF_OPEN:
                logger.info("Circuit breaker transitioning OPEN (failure in half-open)")
                self._transition_to(CircuitState.OPEN)
            elif self._state == CircuitState.CLOSED:
                failures = self._count_recent_failures()
                if failures >= self.failure_threshold:
                    logger.warning(
                        "Circuit breaker transitioning OPEN (failures=%d >= threshold=%d)",
                        failures,
                        self.failure_threshold,
                    )
                    self._transition_to(CircuitState.OPEN)

    async def reset(self) -> None:
        """Force-reset the circuit breaker to CLOSED."""
        async with self._lock:
            self._results.clear()
            self._half_open_successes = 0
            self._transition_to(CircuitState.CLOSED)
            logger.info("Circuit breaker force-reset to CLOSED")

    # ------------------------------------------------------------------
    # Core async wrapper
    # ------------------------------------------------------------------

    async def call(
        self,
        func: Callable[..., Coroutine[Any, Any, Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute *func* through the circuit breaker.

        Raises CircuitOpenError when the circuit is open.
        Tracks timing and records success/failure automatically.
        """
        current = await self.state()

        if current == CircuitState.OPEN:
            raise CircuitOpenError(
                f"Circuit is open; retry after {self.timeout - (time.monotonic() - self._opened_at):.1f}s"
            )

        start = time.monotonic()
        try:
            result = await func(*args, **kwargs)
            elapsed = time.monotonic() - start

            if elapsed > self.slow_call_threshold:
                logger.warning(
                    "Slow call detected (%.1fs > %.1fs threshold), recording as failure",
                    elapsed,
                    self.slow_call_threshold,
                )
                await self.record_failure()
            else:
                await self.record_success()

            return result
        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.debug("Circuit breaker caught exception after %.2fs: %s", elapsed, exc)
            await self.record_failure()
            raise

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _push(self, success: bool) -> None:
        self._results.append((time.monotonic(), success))
        # Trim the sliding window
        if len(self._results) > self._window_size:
            self._results = self._results[-self._window_size:]

    def _count_recent_failures(self) -> int:
        """Count failures in the sliding window (last N results)."""
        return sum(1 for _, ok in self._results if not ok)

    def _transition_to(self, new_state: CircuitState) -> None:
        old = self._state
        self._state = new_state
        if new_state == CircuitState.OPEN:
            self._opened_at = time.monotonic()
            self._half_open_successes = 0
        elif new_state == CircuitState.HALF_OPEN:
            self._half_open_successes = 0
        elif new_state == CircuitState.CLOSED:
            self._results.clear()
            self._half_open_successes = 0
        logger.info("Circuit breaker state: %s -> %s", old.value, new_state.value)
