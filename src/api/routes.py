"""FastAPI 路由"""
import asyncio
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import FileResponse

from src.api.models import (
    ChatRequest, TaskResponse, ArtifactInfo, AgentInfo, HealthResponse,
)
from src.api.websocket import ws_manager
from src.config import settings
from src.api.models import TaskStatus
from src.task_manager.manager import TaskManager
from src.orchestrator.orchestrator import Orchestrator
from src.container_pool.pool import ContainerPoolManager

logger = logging.getLogger(__name__)

router = APIRouter()

# 这些会在 main.py 的 lifespan 中初始化
orchestrator: Optional[Orchestrator] = None
task_manager: Optional[TaskManager] = None
pool_manager: Optional[ContainerPoolManager] = None


def set_deps(orch: Orchestrator, tm: TaskManager, pool: ContainerPoolManager):
    global orchestrator, task_manager, pool_manager
    orchestrator = orch
    task_manager = tm
    pool_manager = pool


@router.post("/api/chat", response_model=TaskResponse)
async def chat(req: ChatRequest):
    """接收用户消息，创建并执行任务"""
    # 创建任务
    task = await task_manager.create_task(
        user_message=req.message,
        tenant_id=req.tenant_id or "default",
    )
    
    # 订阅事件到 WebSocket 广播
    async def on_event(event: dict):
        await ws_manager.broadcast(task.task_id, event)
    
    task.subscribe(on_event)
    
    # 更新状态为运行中
    await task_manager.update_status(task.task_id, TaskStatus.RUNNING)
    
    # 在后台执行编排
    async def run_orchestration():
        try:
            result = await orchestrator.execute(
                task_id=task.task_id,
                tenant_id=task.tenant_id,
                user_message=req.message,
                event_callback=on_event,
            )
            await task_manager.complete_task(task.task_id, result)
        except Exception as e:
            logger.error(f"Orchestration failed: {e}", exc_info=True)
            await task_manager.fail_task(task.task_id, str(e))
    
    asyncio.create_task(run_orchestration())
    
    return TaskResponse(
        task_id=task.task_id,
        status=TaskStatus.RUNNING,
        message="任务已创建，正在执行中...",
    )


@router.get("/api/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    """查询任务状态"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    
    return TaskResponse(
        task_id=task.task_id,
        status=task.status,
        message=task.result,
        artifacts=task.artifacts,
    )


@router.get("/api/tasks", response_model=list[TaskResponse])
async def list_tasks(tenant_id: Optional[str] = None):
    """列举任务"""
    tasks = task_manager.list_tasks(tenant_id)
    return [
        TaskResponse(
            task_id=t.task_id,
            status=t.status,
            message=t.result,
            artifacts=t.artifacts,
        )
        for t in tasks
    ]


@router.get("/api/tasks/{task_id}/artifacts", response_model=list[ArtifactInfo])
async def list_artifacts(task_id: str):
    """列出任务产物"""
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    
    artifacts = []
    if task.work_dir:
        for f in task.work_dir.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(task.work_dir))
                artifacts.append(ArtifactInfo(
                    name=rel,
                    path=rel,
                    size=f.stat().st_size,
                ))
    
    return artifacts


@router.get("/api/tasks/{task_id}/download")
async def download_artifacts(task_id: str):
    """下载任务产物压缩包"""
    zip_path = await task_manager.create_artifact_zip(task_id)
    if not zip_path:
        raise HTTPException(404, "No artifacts to download")
    
    return FileResponse(
        path=zip_path,
        filename=f"task_{task_id}_artifacts.zip",
        media_type="application/zip",
    )


@router.get("/api/agents", response_model=list[AgentInfo])
async def list_agents():
    """列出可用 Agent 类型"""
    return [
        AgentInfo(
            id=ac.id,
            name=ac.name,
            description=ac.description,
            skills=ac.skills,
        )
        for ac in settings.agent_cards
    ]


@router.get("/api/health", response_model=HealthResponse)
async def health():
    """健康检查"""
    pool_status = pool_manager.get_status() if pool_manager else {"total": 0, "idle": 0}
    return HealthResponse(
        status="ok",
        pool_available=pool_status.get("idle", 0),
        pool_total=pool_status.get("total", 0),
        active_tasks=sum(1 for t in (task_manager.list_tasks() if task_manager else []) 
                        if t.status == TaskStatus.RUNNING),
    )


@router.websocket("/ws/tasks/{task_id}")
async def task_websocket(websocket: WebSocket, task_id: str):
    """WebSocket 实时任务事件流"""
    await ws_manager.connect(websocket, task_id)
    try:
        # 发送初始状态
        task = task_manager.get_task(task_id)
        if task:
            await websocket.send_json({
                "type": "status",
                "task_id": task_id,
                "status": task.status.value,
            })
        
        # 保持连接，监听客户端消息（可用于取消等）
        while True:
            data = await websocket.receive_json()
            if data.get("action") == "cancel":
                await task_manager.update_status(task_id, TaskStatus.CANCELLED)
                break
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(websocket, task_id)
