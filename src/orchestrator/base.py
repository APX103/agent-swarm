"""Pluggable orchestrator contract (Round 3).

The orchestration loop (plan -> dispatch -> review -> finalize) is a selectable
backend. The built-in ``Orchestrator`` is the default; an external A2A scheduler
agent can take over. Selection + fallback live in :mod:`src.orchestrator.resolver`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Protocol, runtime_checkable

# A coroutine receiving a task event dict (used for WebSocket broadcast).
EventCallback = Optional[Callable[[dict], Awaitable[None]]]


@runtime_checkable
class OrchestratorBackend(Protocol):
    """Anything that can run an orchestration loop for a user message."""

    async def execute(
        self,
        task_id: str,
        tenant_id: str,
        user_message: str,
        event_callback: EventCallback = None,
    ) -> str: ...


@dataclass
class OrchestratorConfig:
    """Selection policy for the active orchestrator backend."""

    provider: str = "builtin"  # "builtin" | "external"
    external_endpoint: str = ""  # URL of an external A2A scheduler agent
    external_timeout: float = 600.0
    fallback: bool = True  # auto-fallback to builtin on external failure
