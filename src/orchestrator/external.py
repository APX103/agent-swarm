"""External orchestrator — delegates the whole loop to an A2A scheduler agent.

Production path:
- Forwards session context (work_dir/task_id/tenant_id) via A2A configuration.
- Forwards conversation history (compressed) so the external agent has memory.
- Runs non-blocking + polls for streaming progress, forwarded to event_callback.
- Writes structured events to SessionService for audit/replay.
"""
from __future__ import annotations

import logging
from typing import Optional

from src.common.a2a_client import A2AClient, A2AMessage
from src.orchestrator.base import EventCallback

logger = logging.getLogger(__name__)

# 历史消息最多压缩多少条传给外部 orchestrator（避免 context 爆炸）
_MAX_HISTORY_TURNS = 10


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
        event_callback: Optional[EventCallback] = None,
        session=None,
        session_service=None,
    ) -> str:
        client = A2AClient(self._endpoint, timeout=self._timeout)
        try:
            # ── 组装传给外部 orchestrator 的 configuration ─────────────────────
            config: dict = {
                "task_id": task_id,
                "tenant_id": tenant_id,
            }

            # session 上下文：work_dir（产物路径）+ session_id
            sess_id: Optional[str] = None
            if session is not None:
                work_dir = getattr(session, "work_dir", None)
                if work_dir:
                    config["shared_dir"] = str(work_dir)
                sess_id = getattr(session, "session_id", None)
                if sess_id:
                    config["session_id"] = sess_id

                # ── 多轮记忆：把历史对话压缩成文本，放进 user message 前缀 ──────
                # 这样外部 orchestrator（如 eino）能"看到"之前的对话上下文。
                messages = getattr(session, "messages", None) or []
                # 过滤掉 system 消息和空的，只取最近的 N 轮 user/assistant
                history = [
                    m for m in messages
                    if isinstance(m, dict)
                    and m.get("role") in ("user", "assistant")
                    and m.get("content")
                ][-_MAX_HISTORY_TURNS:]
                if history:
                    history_text = self._compress_history(history)
                    user_message = f"{history_text}\n\n---\n\n## 本次请求\n{user_message}"

            # ── 写 session 事件：orchestrator_started ──────────────────────────
            if session_service and sess_id:
                await session_service.append_event(sess_id, {
                    "type": "orchestrator_started",
                    "provider": "external",
                    "endpoint": self._endpoint,
                })

            if event_callback:
                await event_callback({
                    "type": "orchestrator_thinking",
                    "task_id": task_id,
                    "agent": "external",
                    "data": {"provider": "external", "endpoint": self._endpoint},
                })

            # ── 非阻塞 + 轮询 ──────────────────────────────────────────────────
            task = await client.send_message(
                A2AMessage(role="user", text=user_message),
                blocking=False,
                configuration=config,
            )

            if task is not None:
                async for snap in client.poll_task(
                    task.task_id, interval=2.0, timeout=self._timeout
                ):
                    if event_callback:
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
                    task = snap
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
            # 写 session 事件：orchestrator_failed
            if session_service and sess_id:
                await session_service.append_event(sess_id, {
                    "type": "orchestrator_failed",
                    "error": str(e)[:200],
                })
            raise
        finally:
            try:
                await client.close()
            except Exception:
                logger.warning("Error closing external orchestrator A2A client", exc_info=True)

        if task is None:
            if session_service and sess_id:
                await session_service.append_event(sess_id, {
                    "type": "orchestrator_failed", "error": "returned no task",
                })
            raise RuntimeError("External orchestrator returned no task")
        if task.state == "failed":
            if session_service and sess_id:
                await session_service.append_event(sess_id, {
                    "type": "orchestrator_failed",
                    "error": task.message or "task failed",
                })
            raise RuntimeError(
                f"External orchestrator task failed: {task.message or 'no detail'}"
            )

        # ── 写 session 事件：orchestrator_completed + 存结果 ──────────────────
        if session_service and sess_id:
            await session_service.append_event(sess_id, {
                "type": "orchestrator_completed",
                "result": (task.message or "")[:500],
            })
            await session_service.update_state(sess_id, {
                "last_result": (task.message or "")[:2000],
            })

        # ── 更新 session 的对话历史（让下一轮能"接着聊"）──────────────────────
        if session is not None:
            messages = getattr(session, "messages", None)
            if isinstance(messages, list):
                messages.append({"role": "user", "content": user_message.split("## 本次请求\n")[-1] if "## 本次请求" in user_message else user_message})
                messages.append({"role": "assistant", "content": task.message or ""})

        return task.message or ""

    @staticmethod
    def _compress_history(history: list[dict]) -> str:
        """把历史消息列表压缩成简洁的文本摘要。"""
        lines = ["## 之前的对话历史（供参考）\n"]
        for m in history:
            role = m["role"]
            content = m["content"]
            # 截断过长的单条消息
            if isinstance(content, list):
                # OpenAI 消息格式（content 可能是 list of parts）
                content = " ".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            if not isinstance(content, str):
                content = str(content)
            if len(content) > 500:
                content = content[:500] + "..."
            label = "用户" if role == "user" else "助手"
            lines.append(f"**{label}**: {content}\n")
        return "\n".join(lines)
