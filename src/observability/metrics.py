"""Lightweight in-memory metrics for dispatch observability."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class Metrics:
    """Thread-safe counters + latency tracking for dispatch operations."""

    dispatch_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    total_latency_ms: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_dispatch(self, success: bool, latency_ms: float) -> None:
        with self._lock:
            self.dispatch_count += 1
            if success:
                self.success_count += 1
            else:
                self.failure_count += 1
            self.total_latency_ms += latency_ms

    def snapshot(self) -> dict:
        with self._lock:
            avg = self.total_latency_ms / self.dispatch_count if self.dispatch_count else 0
            rate = self.failure_count / self.dispatch_count if self.dispatch_count else 0
            return {
                "dispatch_total": self.dispatch_count,
                "success": self.success_count,
                "failure": self.failure_count,
                "avg_latency_ms": round(avg, 1),
                "failure_rate": round(rate, 3),
            }

    def reset(self) -> None:
        with self._lock:
            self.dispatch_count = 0
            self.success_count = 0
            self.failure_count = 0
            self.total_latency_ms = 0.0


# Module-level singleton (shared between Dispatcher + /api/v1/metrics endpoint)
_global = Metrics()


def get_metrics() -> Metrics:
    return _global
