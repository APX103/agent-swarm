"""A2A adapter — wraps an A2A-protocol agent (JSON-RPC ``message/send`` over HTTP).

Replaces the previous ``"a2a" -> OpenAIAdapter`` alias: external agents that speak
the A2A protocol (the same protocol the Docker workers use) are now invoked through
a real A2A client rather than being treated as OpenAI-compatible.
"""
import asyncio
import logging
from typing import Optional

from .base import AgentBackend, AgentCapabilities, AgentResult, ProgressCallback
from src.common.a2a_client import A2AClient, A2AMessage

logger = logging.getLogger(__name__)


class A2AAdapter(AgentBackend):
    """Adapter for A2A-protocol agents."""

    def __init__(self, base_url: str, timeout: int = 300, **kwargs) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: Optional[A2AClient] = None
        self._client_lock = asyncio.Lock()

    async def _get_client(self) -> A2AClient:
        """Return the A2A client, creating it lazily under a lock."""
        if self._client is None:
            async with self._client_lock:
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

    async def invoke(
        self, task: str, context: dict = None, on_progress: Optional[ProgressCallback] = None
    ) -> AgentResult:
        """Send the task as an A2A message and map the result.

        When *on_progress* is supplied, send non-blocking and poll the task,
        forwarding each snapshot (state + message + progress) to the callback —
        mirroring how ``DockerBackend`` streams worker progress. Otherwise block.
        """
        client = await self._get_client()
        try:
            a2a_task = await self._invoke_inner(client, task, on_progress)
        except Exception as e:
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

    async def _invoke_inner(self, client: A2AClient, task: str, on_progress):
        """Blocking send, or non-blocking + poll forwarding, depending on *on_progress*."""
        if on_progress is None:
            return await client.send_message(
                A2AMessage(role="user", text=task), blocking=True
            )

        # streaming path: non-blocking send, then poll + forward snapshots
        a2a_task = await client.send_message(
            A2AMessage(role="user", text=task), blocking=False
        )
        if a2a_task is None:
            return None
        async for snap in client.poll_task(a2a_task.task_id, interval=2.0, timeout=float(self.timeout)):
            try:
                await on_progress({"state": snap.state, "message": snap.message, "progress": snap.progress})
            except Exception:
                logger.warning("progress callback failed for %s", self.base_url, exc_info=True)
            a2a_task = snap
        return a2a_task

    async def health_check(self) -> bool:
        """Healthy if the agent serves an AgentCard."""
        client = await self._get_client()
        try:
            card = await client.get_agent_card()
            return card is not None
        except Exception:
            logger.debug("A2A health check failed for %s", self.base_url, exc_info=True)
            return False
