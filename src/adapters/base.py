import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Any, Optional

logger = logging.getLogger(__name__)


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
    async def invoke(self, task: str, context: dict = None) -> AgentResult: ...

    @abstractmethod
    async def health_check(self) -> bool: ...

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities()
