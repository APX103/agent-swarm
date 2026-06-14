"""External orchestrator — delegates the whole loop to an A2A scheduler agent."""
from __future__ import annotations

import logging

from src.common.a2a_client import A2AClient, A2AMessage
from src.orchestrator.base import EventCallback

logger = logging.getLogger(__name__)


class ExternalOrchestrator:
    """Runs orchestration by forwarding the user message to an external scheduler agent.

    Failures (no task, failed state, transport error) raise so the resolver can
    fall back to the builtin orchestrator.
    """

    def __init__(self, endpoint: str, timeout: float = 600.0) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout

    async def execute(
        self,
        task_id: str,
        tenant_id: str,
        user_message: str,
        event_callback: EventCallback = None,
    ) -> str:
        client = A2AClient(self._endpoint, timeout=self._timeout)
        try:
            task = await client.send_message(
                A2AMessage(role="user", text=user_message), blocking=True
            )
        finally:
            try:
                await client.close()
            except Exception:
                logger.warning("Error closing external orchestrator A2A client", exc_info=True)

        if task is None:
            raise RuntimeError("External orchestrator returned no task")
        if task.state == "failed":
            raise RuntimeError(
                f"External orchestrator task failed: {task.message or 'no detail'}"
            )
        return task.message or ""
