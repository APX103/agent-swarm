"""API Pydantic 模型"""
from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum


class TaskStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChatRequest(BaseModel):
    """用户聊天请求"""
    message: str = Field(..., description="用户消息内容")
    tenant_id: Optional[str] = Field(None, description="租户 ID（多租户场景）")


class TaskResponse(BaseModel):
    """任务状态响应"""
    task_id: str
    status: TaskStatus
    message: Optional[str] = None
    artifacts: list[str] = Field(default_factory=list)


class ArtifactInfo(BaseModel):
    """产物信息"""
    name: str
    path: str
    size: int


class AgentInfo(BaseModel):
    """Agent 信息（来自 AgentCard）"""
    id: str
    name: str
    description: str
    skills: list[dict] = Field(default_factory=list)


class WSEvent(BaseModel):
    """WebSocket 事件"""
    type: str
    task_id: str
    agent: Optional[str] = None
    data: Optional[dict | str] = None


class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str = "ok"
    pool_available: int = 0
    pool_total: int = 0
    active_tasks: int = 0
