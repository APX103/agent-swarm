"""Dead-letter store for failed orchestrations.

When an orchestration fails, a record is kept (task id, tenant, error, user
message, timestamp) so it can be inspected or replayed. Bounded in-memory; a
persistent/Redis-backed store is future work.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque


@dataclass
class DeadLetterRecord:
    task_id: str
    tenant_id: str
    error: str
    user_message: str
    timestamp: float = field(default_factory=time.time)


class DeadLetterStore:
    """Bounded, in-memory dead-letter store (oldest entries evicted first)."""

    def __init__(self, max_size: int = 256) -> None:
        self._records: Deque[DeadLetterRecord] = deque(maxlen=max_size)
        self._lock = asyncio.Lock()

    async def record(self, rec: DeadLetterRecord) -> None:
        async with self._lock:
            self._records.append(rec)

    async def recent(self, n: int = 50) -> list[DeadLetterRecord]:
        """Return up to the *n* most recent records (newest last)."""
        async with self._lock:
            if n <= 0:
                return []
            return list(self._records)[-n:]

    async def all(self) -> list[DeadLetterRecord]:
        async with self._lock:
            return list(self._records)

    async def clear(self) -> None:
        async with self._lock:
            self._records.clear()
