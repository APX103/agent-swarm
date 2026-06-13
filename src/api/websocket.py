"""WebSocket 连接管理器"""
import asyncio
import json
import logging
from typing import Optional
from dataclasses import dataclass

from fastapi import WebSocket

logger = logging.getLogger(__name__)


@dataclass
class WSConnection:
    websocket: WebSocket
    task_id: str


class WSConnectionManager:
    """管理 WebSocket 连接，支持任务事件广播"""
    
    def __init__(self):
        self._connections: dict[str, list[WSConnection]] = {}  # task_id -> connections
    
    async def connect(self, websocket: WebSocket, task_id: str):
        """接受 WebSocket 连接"""
        await websocket.accept()
        conn = WSConnection(websocket=websocket, task_id=task_id)
        if task_id not in self._connections:
            self._connections[task_id] = []
        self._connections[task_id].append(conn)
        logger.debug(f"WS connected for task {task_id}")
    
    def disconnect(self, websocket: WebSocket, task_id: str):
        """断开 WebSocket 连接"""
        if task_id in self._connections:
            self._connections[task_id] = [
                c for c in self._connections[task_id] 
                if c.websocket != websocket
            ]
            if not self._connections[task_id]:
                del self._connections[task_id]
    
    async def broadcast(self, task_id: str, event: dict):
        """向任务的所有 WebSocket 订阅者广播事件"""
        connections = self._connections.get(task_id, [])
        dead = []
        
        for conn in connections:
            try:
                await conn.websocket.send_json(event)
            except Exception:
                dead.append(conn)
        
        # 清理断开的连接
        for conn in dead:
            self.disconnect(conn.websocket, task_id)
    
    def connection_count(self, task_id: str) -> int:
        return len(self._connections.get(task_id, []))


# 全局单例
ws_manager = WSConnectionManager()
