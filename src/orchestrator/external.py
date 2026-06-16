"""External orchestrator — delegates the whole loop to an A2A scheduler agent.

Production path: forwards session context (work_dir/task_id/tenant_id) via the
A2A configuration field, runs non-blocking + polls for streaming progress, and
forwards each snapshot to event_callback so the WebSocket UI is live.
"""
from __future__ import annotations

import logging
from typing import Optional

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
        session=None,
    ) -> str:
        client = A2AClient(self._endpoint, timeout=self._timeout)
        try:
            # ── 限制1: 传 session 上下文给外部 orchestrator ────────────────────
            # 外部 orchestrator（如 eino）需要 work_dir 才能让它的 dispatch tool
            # 把 shared_dir 传给 worker，使产物落到正确的 task 目录。
            config = {
                "task_id": task_id,
                "tenant_id": tenant_id,
            }
            if session is not None:
                work_dir = getattr(session, "work_dir", None)
                if work_dir:
                    config["shared_dir"] = str(work_dir)
                sess_id = getattr(session, "session_id", None)
                if sess_id:
                    config["session_id"] = sess_id

            if event_callback:
                await event_callback({
                    "type": "orchestrator_thinking",
                    "task_id": task_id,
                    "agent": "external",
                    "data": {"provider": "external", "endpoint": self._endpoint},
                })

            # ── 限制2: 非阻塞 + 轮询，转发进度到 WebSocket ─────────────────────
            task = await client.send_message(
                A2AMessage(role="user", text=user_message),
                blocking=False,
                configuration=config,
            )

            if task is not None:
                # 轮询直到终态，每个快照转发给 event_callback
                async for snap in client.poll_task(
                    task.task_id, interval=2.0, timeout=self._timeout
                ):
                    if event_callback:
                        # 把外部 orchestrator 的进度转发为 agent_progress 事件
                        progress = getattr(snap, "progress", None) or []
                        await event_callback({
                            "type": "agent_progress",
                            "task_id": task_id,
                            "agent": "external",
                            "data": {
                                "state": snap.state,
                                "message": snap.message or "",
                                "progress": progress,
                            },
                        })
                    task = snap  # 保留最后一个快照
            else:
                # 外部 orchestrator 不支持 non-blocking，回退到 blocking
                logger.warning("External orchestrator did not return a task on non-blocking send; retrying blocking")
                task = await client.send_message(
                    A2AMessage(role="user", text=user_message),
                    blocking=True,
                    configuration=config,
                )
        except Exception as e:
            logger.warning("External orchestrator failed: %s", e, exc_info=True)
            raise
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
