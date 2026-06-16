"""OrchestratorResolver — selects the active orchestrator backend, with fallback."""
from __future__ import annotations

import logging
from typing import Optional

from src.orchestrator.base import EventCallback, OrchestratorBackend, OrchestratorConfig
from src.orchestrator.external import ExternalOrchestrator

logger = logging.getLogger(__name__)


class OrchestratorResolver:
    """Picks the active orchestrator per config; falls back to builtin on external failure.

    The fallback is explicit, never silent: it emits an ``orchestrator_fallback``
    event (when an event_callback is supplied) and logs at WARNING/INFO.
    """

    def __init__(self, builtin: OrchestratorBackend, config: OrchestratorConfig) -> None:
        self._builtin = builtin
        self._config = config

    def _build_external(self) -> Optional[OrchestratorBackend]:
        if not self._config.external_endpoint:
            return None
        return ExternalOrchestrator(
            endpoint=self._config.external_endpoint,
            timeout=self._config.external_timeout,
        )

    async def execute(
        self,
        task_id: str,
        tenant_id: str,
        user_message: str,
        event_callback: EventCallback = None,
        session=None,
    ) -> str:
        if self._config.provider == "external":
            external = self._build_external()
            if external is not None:
                try:
                    return await external.execute(
                        task_id, tenant_id, user_message, event_callback, session
                    )
                except Exception as e:
                    logger.warning("External orchestrator failed: %s", e, exc_info=True)
                    await self._emit_fallback(event_callback, task_id, str(e))
                    if not self._config.fallback:
                        raise
                    logger.info("Falling back to builtin orchestrator")
            else:
                msg = "External orchestrator provider set but no external_endpoint configured"
                logger.warning(msg)
                await self._emit_fallback(event_callback, task_id, msg)
                if not self._config.fallback:
                    raise RuntimeError(msg)
                logger.info("Falling back to builtin orchestrator")

        if session is not None:
            return await self._builtin.execute(task_id, tenant_id, user_message, event_callback, session=session)
        return await self._builtin.execute(task_id, tenant_id, user_message, event_callback)

    async def _emit_fallback(
        self, event_callback: EventCallback, task_id: str, reason: str
    ) -> None:
        if event_callback is None:
            return
        try:
            await event_callback(
                {
                    "type": "orchestrator_fallback",
                    "task_id": task_id,
                    "data": {"reason": reason},
                }
            )
        except Exception:
            logger.warning("fallback event callback failed", exc_info=True)
