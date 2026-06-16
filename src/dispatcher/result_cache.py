"""Result cache for graceful degradation (L2).

Successful dispatch results are cached by (agent_type, task). When every candidate
fails, a cached hit is returned as a degraded success instead of a hard failure.
Only successes are cached; entries expire after ``ttl`` seconds.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from src.dispatcher.base import DispatchResult

logger = logging.getLogger(__name__)


class ResultCache:
    """Bounded, TTL-based in-memory cache of successful DispatchResults."""

    def __init__(self, ttl: float = 3600.0, max_size: int = 256) -> None:
        self._ttl = ttl
        self._max_size = max_size
        self._store: dict[str, tuple[DispatchResult, float]] = {}

    @staticmethod
    def _key(agent_type: str, task: str, task_id: str = "") -> str:
        # Include task_id in the key so identical prompts from different tasks
        # don't return each other's cached results.
        return f"{agent_type}:{hash(task)}:{task_id}"

    def get(self, agent_type: str, task: str, task_id: str = "") -> Optional[DispatchResult]:
        key = self._key(agent_type, task, task_id)
        entry = self._store.get(key)
        if entry is None:
            return None
        result, expires_at = entry
        if time.time() > expires_at:
            self._store.pop(key, None)
            return None
        return result

    def put(self, agent_type: str, task: str, result: DispatchResult, task_id: str = "") -> None:
        if not result.success:
            return  # never cache failures
        if len(self._store) >= self._max_size:
            # evict the entry with the nearest expiry
            oldest_key = min(self._store, key=lambda k: self._store[k][1])
            self._store.pop(oldest_key, None)
        self._store[self._key(agent_type, task, task_id)] = (result, time.time() + self._ttl)
