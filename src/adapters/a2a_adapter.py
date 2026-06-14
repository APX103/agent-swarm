"""A2A adapter — wraps an A2A-protocol agent (JSON-RPC ``message/send`` over HTTP).

Replaces the previous ``"a2a" -> OpenAIAdapter`` alias: external agents that speak
the A2A protocol (the same protocol the Docker workers use) are now invoked through
a real A2A client rather than being treated as OpenAI-compatible.
"""
import logging
from typing import Optional

from .base import AgentBackend, AgentCapabilities, AgentResult
from src.common.a2a_client import A2AClient, A2AMessage

logger = logging.getLogger(__name__)


class A2AAdapter(AgentBackend):
    """Adapter for A2A-protocol agents."""

    def __init__(self, base_url: str, timeout: int = 300) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: Optional[A2AClient] = None

    def _get_client(self) -> A2AClient:
        if self._client is None:
            self._client = A2AClient(self.base_url, timeout=float(self.timeout))
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                logger.warning("Error closing A2A client for %s", self.base_url, exc_info=True)
            self._client = None

    @property
    def name(self) -> str:
        return f"a2a:{self.base_url}"

    @property
    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            skills=["a2a"],
            input_modes=["text"],
            output_modes=["text", "artifacts"],
        )

    async def invoke(self, task: str, context: dict = None) -> AgentResult:
        """Send the task as an A2A message (blocking) and map the result."""
        client = self._get_client()
        try:
            a2a_task = await client.send_message(
                A2AMessage(role="user", text=task), blocking=True
            )
        except Exception as e:
            # A2AClient swallows most errors and returns None, but be defensive.
            logger.exception("A2A send failed for %s", self.base_url)
            return AgentResult(success=False, output="", error=f"A2A send failed: {e!s}")

        if a2a_task is None:
            return AgentResult(success=False, output="", error="A2A agent returned no task")

        success = a2a_task.state == "completed"
        return AgentResult(
            success=success,
            output=a2a_task.message or "",
            artifacts=[str(a) for a in (a2a_task.artifacts or [])],
            error=None if success else (f"A2A task state: {a2a_task.state}"),
            metadata={"task_id": a2a_task.task_id, "state": a2a_task.state},
        )

    async def health_check(self) -> bool:
        """Healthy if the agent serves an AgentCard."""
        client = self._get_client()
        try:
            card = await client.get_agent_card()
            return card is not None
        except Exception:
            logger.debug("A2A health check failed for %s", self.base_url, exc_info=True)
            return False
