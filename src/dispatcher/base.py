"""Dispatcher protocol + dispatch dataclasses (Round 2 foundation).

These types define the contract for the unified dispatch path: the orchestrator
hands the Dispatcher a :class:`DispatchRequest`, and receives a
:class:`DispatchResult` whose ``attempts`` record every candidate tried (Docker
containers and/or externally-registered agents).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal, Optional, Protocol


class TargetKind:
    """Discriminator for where a dispatch target lives."""

    DOCKER = "docker"
    EXTERNAL = "external"


@dataclass
class DispatchTarget:
    """A single candidate that can serve a dispatch request.

    ``kind`` is ``"docker"`` (a pooled container built from an agent card) or
    ``"external"`` (a registered external agent reached via its adapter).
    """

    kind: Literal["docker", "external"]
    agent_type: str
    agent_id: Optional[str] = None
    endpoint: Optional[str] = None


@dataclass
class DispatchRequest:
    """A unit of work the dispatcher routes to a suitable agent."""

    agent_type: str
    task: str
    context: dict = field(default_factory=dict)
    timeout: Optional[float] = None
    # Optional async progress callback (streaming). When set, streaming-capable
    # backends send non-blocking and forward worker snapshots here.
    on_progress: Optional[Callable[[dict], Awaitable[None]]] = None
    # Optional: when set, backends that support direct selection route to this
    # specific agent_id instead of doing skill-based matching (used by direct-chat).
    agent_id: Optional[str] = None


@dataclass
class DispatchAttempt:
    """Outcome of trying a single candidate."""

    target: DispatchTarget
    success: bool
    output: str = ""
    error: Optional[str] = None


@dataclass
class DispatchResult:
    """Final outcome of a dispatch request."""

    success: bool
    output: str = ""
    artifacts: list[str] = field(default_factory=list)
    error: Optional[str] = None
    target: Optional[DispatchTarget] = None
    attempts: list[DispatchAttempt] = field(default_factory=list)
    degraded: bool = False  # True when served from the result cache (L2 fallback)

    @classmethod
    def from_attempt(cls, attempt: DispatchAttempt, artifacts: Optional[list[str]] = None) -> "DispatchResult":
        """Build a successful/failed result mirroring a single attempt."""
        return cls(
            success=attempt.success,
            output=attempt.output,
            artifacts=artifacts or [],
            error=attempt.error,
            target=attempt.target,
            attempts=[attempt],
        )


class Dispatcher(Protocol):
    """Structural protocol: anything that can route a DispatchRequest."""

    async def dispatch(self, request: DispatchRequest) -> DispatchResult: ...
