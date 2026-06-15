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
    _per_agent: dict = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_dispatch(self, success: bool, latency_ms: float, agent_type: str = "") -> None:
        with self._lock:
            self.dispatch_count += 1
            if success:
                self.success_count += 1
            else:
                self.failure_count += 1
            self.total_latency_ms += latency_ms
            if agent_type:
                if agent_type not in self._per_agent:
                    self._per_agent[agent_type] = {"total": 0, "success": 0, "failure": 0, "total_latency_ms": 0.0}
                a = self._per_agent[agent_type]
                a["total"] += 1
                if success:
                    a["success"] += 1
                else:
                    a["failure"] += 1
                a["total_latency_ms"] += latency_ms

    def snapshot(self) -> dict:
        with self._lock:
            avg = self.total_latency_ms / self.dispatch_count if self.dispatch_count else 0
            rate = self.failure_count / self.dispatch_count if self.dispatch_count else 0
            per_agent = {}
            for at, a in self._per_agent.items():
                a_avg = a["total_latency_ms"] / a["total"] if a["total"] else 0
                per_agent[at] = {
                    "total": a["total"], "success": a["success"], "failure": a["failure"],
                    "avg_latency_ms": round(a_avg, 1),
                }
            return {
                "dispatch_total": self.dispatch_count,
                "success": self.success_count,
                "failure": self.failure_count,
                "avg_latency_ms": round(avg, 1),
                "failure_rate": round(rate, 3),
                "per_agent": per_agent,
            }

    def reset(self) -> None:
        with self._lock:
            self.dispatch_count = 0
            self.success_count = 0
            self.failure_count = 0
            self.total_latency_ms = 0.0
            self._per_agent.clear()


# Module-level singleton (shared between Dispatcher + /api/v1/metrics endpoint)
_global = Metrics()


def get_metrics() -> Metrics:
    return _global
