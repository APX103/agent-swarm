import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Progress callback shape: receives a dict snapshot mid-invoke. Optional across
# all backends; only streaming-capable ones (a2a) act on it.
ProgressCallback = Callable[[dict], Awaitable[None]]


@dataclass
class AgentResult:
    success: bool
    output: str
    artifacts: list[str] = field(default_factory=list)
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class AgentCapabilities:
    skills: list[str] = field(default_factory=list)
    input_modes: list[str] = field(default_factory=list)
    output_modes: list[str] = field(default_factory=list)


class AgentBackend(ABC):
    @abstractmethod
    async def invoke(
        self,
        task: str,
        context: Optional[dict] = None,
        on_progress: Optional[ProgressCallback] = None,
    ) -> AgentResult: ...

    @abstractmethod
    async def health_check(self) -> bool: ...

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities()
