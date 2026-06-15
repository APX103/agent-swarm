"""A2A 协议客户端封装 - 用于 Orchestrator 与 Worker Agent 通信"""
import asyncio
import json
import logging
import time
import uuid
from typing import AsyncIterator, Optional
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


@dataclass
class A2AMessage:
    """A2A 消息"""
    role: str  # "user" or "agent"
    text: str
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    
    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "parts": [{"kind": "text", "text": self.text}],
            "messageId": self.message_id,
        }


@dataclass
class A2ATask:
    """A2A 任务响应"""
    task_id: str
    state: str
    message: Optional[str] = None
    artifacts: list[dict] = field(default_factory=list)
    progress: list[dict] = field(default_factory=list)  # worker 写入的逐步进度事件


class A2AClient:
    """A2A 协议客户端
    
    用于 Orchestrator 向 Worker Agent 发送消息、查询任务状态。
    基于 JSON-RPC 2.0 over HTTP。
    """
    
    def __init__(self, base_url: str, timeout: float = 30.0):
        """
        Args:
            base_url: Worker Agent 的 A2A 服务 URL，如 http://worker-1:9001
            timeout: 请求超时
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)
    
    async def get_agent_card(self) -> Optional[dict]:
        """获取 Agent 的 AgentCard"""
        try:
            resp = await self._client.get(f"{self.base_url}/.well-known/agent.json")
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.error(f"Failed to get agent card from {self.base_url}: {e}")
        return None
    
    async def send_message(self, message: A2AMessage, 
                          blocking: bool = True) -> Optional[A2ATask]:
        """发送消息给 Agent
        
        Args:
            message: A2A 消息
            blocking: 是否阻塞等待完成
        
        Returns:
            A2A Task 对象
        """
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {
                "message": message.to_dict(),
                "configuration": {"blocking": blocking},
            }
        }
        
        try:
            resp = await self._client.post(
                self.base_url,
                json=request,
                headers={"Content-Type": "application/json"},
            )
            
            if resp.status_code != 200:
                logger.error(f"A2A send_message error: {resp.status_code} {resp.text}")
                return None
            
            result = resp.json()
            
            # 解析 JSON-RPC 响应
            if "error" in result:
                logger.error(f"A2A error: {result['error']}")
                return None
            
            data = result.get("result", {})
            return A2ATask(
                task_id=data.get("id", ""),
                state=data.get("status", {}).get("state", "unknown"),
                message=self._extract_text(data),
                artifacts=data.get("artifacts", []),
                progress=data.get("progress", []),
            )
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return None
    
    async def get_task(self, task_id: str) -> Optional[A2ATask]:
        """查询任务状态"""
        request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tasks/get",
            "params": {"id": task_id},
        }
        
        try:
            resp = await self._client.post(
                self.base_url,
                json=request,
                headers={"Content-Type": "application/json"},
            )
            
            if resp.status_code != 200:
                return None
            
            result = resp.json()
            data = result.get("result", {})
            
            return A2ATask(
                task_id=data.get("id", task_id),
                state=data.get("status", {}).get("state", "unknown"),
                message=self._extract_text(data),
                artifacts=data.get("artifacts", []),
                progress=data.get("progress", []),
            )
        except Exception as e:
            logger.error(f"Failed to get task: {e}")
            return None
    
    async def poll_task(
        self,
        task_id: str,
        interval: float = 2.0,
        timeout: float = 300.0,
    ) -> AsyncIterator[A2ATask]:
        """Poll ``tasks/get`` and yield task snapshots as they change.

        Yields when the task's state, message, or progress length changes. Stops
        at a terminal state (completed/failed/canceled) or when *timeout* elapses.
        """
        deadline = time.monotonic() + timeout
        last_key: Optional[tuple] = None
        terminal = ("completed", "failed", "canceled")
        while True:
            if time.monotonic() >= deadline:
                return
            task = await self.get_task(task_id)
            if task is not None:
                key = (task.state, task.message, len(task.progress))
                if key != last_key:
                    last_key = key
                    yield task
                if task.state in terminal:
                    return
            await asyncio.sleep(interval)

    async def cancel_task(self, task_id: str) -> bool:
        """发送 tasks/cancel 到 Worker，取消后台执行。"""
        request = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tasks/cancel",
            "params": {"id": task_id},
        }
        try:
            resp = await self._client.post(
                self.base_url,
                json=request,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 200:
                result = resp.json().get("result", {})
                return result.get("status") == "canceled"
        except Exception as e:
            logger.error(f"Failed to cancel task: {e}")
        return False

    async def close(self):
        await self._client.aclose()
    
    def _extract_text(self, data: dict) -> str:
        """从 A2A 响应中提取文本内容"""
        # 从 history 数组中提取 agent 的最后一条消息
        history = data.get("history", [])
        for item in reversed(history):
            if item.get("role") == "agent":
                parts = item.get("parts", [])
                texts = []
                for part in parts:
                    if part.get("kind") == "text":
                        texts.append(part.get("text", ""))
                return " ".join(texts) if texts else ""
        return ""
