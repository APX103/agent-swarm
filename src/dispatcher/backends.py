"""Concrete dispatch backends.

- :class:`DockerBackend` serves agent types backed by the warm container pool (A2A).
- :class:`ExternalAgentBackend` serves externally-registered agents via their adapters.

Both implement the :class:`DispatchBackend` shape (candidates + invoke + health_check)
so the Dispatcher can treat them uniformly.
"""
from __future__ import annotations

import logging
from typing import Optional, Protocol

from src.common.a2a_client import A2AClient, A2AMessage
from src.dispatcher.base import DispatchAttempt, DispatchRequest, DispatchTarget, TargetKind

logger = logging.getLogger(__name__)


class DispatchBackend(Protocol):
    """Structural shape every dispatch backend implements."""

    async def candidates(self, agent_type: str) -> list[DispatchTarget]: ...

    async def invoke(self, target: DispatchTarget, request: DispatchRequest) -> DispatchAttempt: ...

    async def health_check(self, target: DispatchTarget) -> bool: ...


class DockerBackend:
    """Dispatch to Docker-backed workers via the container pool + A2A."""

    def __init__(self, pool, model: str, base_url: str, api_key: str, worker_host: str = "localhost") -> None:
        self.pool = pool
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._worker_host = worker_host

    async def candidates(self, agent_type: str) -> list[DispatchTarget]:
        # The pool manages multiple containers internally; expose one logical target.
        return [DispatchTarget(kind=TargetKind.DOCKER, agent_type=agent_type)]

    async def invoke(self, target: DispatchTarget, request: DispatchRequest) -> DispatchAttempt:
        task_id = request.context.get("task_id")
        tenant_id = request.context.get("tenant_id")
        container = await self.pool.checkout(
            agent_card_id=target.agent_type,
            task_id=task_id,
            model=self._model,
            base_url=self._base_url,
            api_key=self._api_key,
            tenant_id=tenant_id,
        )
        if container is None:
            return DispatchAttempt(
                target=target, success=False, error="No idle worker container available"
            )

        client = A2AClient(f"http://{self._worker_host}:{container.port}", timeout=300.0)
        a2a_task = None
        send_error: Optional[str] = None
        try:
            if request.on_progress is not None:
                # streaming: send non-blocking, then poll and forward snapshots
                a2a_task = await client.send_message(
                    A2AMessage(role="user", text=request.task), blocking=False
                )
                if a2a_task is not None:
                    async for snap in client.poll_task(a2a_task.task_id, interval=2.0, timeout=300.0):
                        try:
                            await request.on_progress({"state": snap.state, "message": snap.message})
                        except Exception:
                            logger.warning("progress callback failed", exc_info=True)
                        a2a_task = snap
            else:
                a2a_task = await client.send_message(
                    A2AMessage(role="user", text=request.task), blocking=True
                )
        except Exception as e:
            logger.exception("Docker A2A send failed for %s", target.agent_type)
            send_error = f"A2A send error: {e!s}"
        finally:
            try:
                await client.close()
            except Exception:
                logger.warning("Error closing A2A client", exc_info=True)
            try:
                await self.pool.return_container(container.container_id)
            except Exception:
                logger.warning(
                    "Error returning container %s", container.container_id, exc_info=True
                )

        if a2a_task is None:
            return DispatchAttempt(
                target=target, success=False, error=send_error or "Worker returned no task"
            )

        success = a2a_task.state == "completed"
        return DispatchAttempt(
            target=target,
            success=success,
            output=a2a_task.message or "",
            error=None if success else f"Worker task state: {a2a_task.state}",
        )

    async def health_check(self, target: DispatchTarget) -> bool:
        # Container readiness is enforced inside checkout(); treat as healthy.
        return True


class ExternalAgentBackend:
    """Dispatch to externally-registered agents through their adapters."""

    def __init__(self, registry, adapter_manager) -> None:
        self.registry = registry
        self.adapter_manager = adapter_manager

    async def candidates(self, agent_type: str) -> list[DispatchTarget]:
        try:
            agents = await self.registry.find_by_skill(agent_type)
        except Exception:
            logger.warning("find_by_skill(%s) failed", agent_type, exc_info=True)
            return []
        targets: list[DispatchTarget] = []
        for a in agents or []:
            agent_id = a.get("id")
            if not agent_id:
                continue
            targets.append(
                DispatchTarget(
                    kind=TargetKind.EXTERNAL,
                    agent_type=agent_type,
                    agent_id=agent_id,
                    endpoint=a.get("endpoint"),
                )
            )
        return targets

    async def invoke(self, target: DispatchTarget, request: DispatchRequest) -> DispatchAttempt:
        adapter = self.adapter_manager.get(target.agent_id)
        if adapter is None:
            return DispatchAttempt(
                target=target,
                success=False,
                error=f"No adapter for external agent {target.agent_id}",
            )
        try:
            result = await adapter.invoke(request.task, request.context)
        except Exception as e:
            logger.exception("External adapter invoke failed for %s", target.agent_id)
            return DispatchAttempt(target=target, success=False, error=str(e))
        return DispatchAttempt(
            target=target,
            success=result.success,
            output=getattr(result, "output", ""),
            error=getattr(result, "error", None),
        )

    async def health_check(self, target: DispatchTarget) -> bool:
        adapter = self.adapter_manager.get(target.agent_id)
        if adapter is None:
            return False
        try:
            return bool(await adapter.health_check())
        except Exception:
            logger.debug("External health check failed for %s", target.agent_id, exc_info=True)
            return False
