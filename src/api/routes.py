"""FastAPI 路由"""
import asyncio
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, Query, Header
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.api.models import (
    ChatRequest, TaskResponse, ArtifactInfo, AgentInfo, HealthResponse,
)
from src.api.websocket import ws_manager
from src.config import settings
from src.api.models import TaskStatus
from src.task_manager.manager import TaskManager
from src.orchestrator.orchestrator import Orchestrator
from src.container_pool.pool import ContainerPoolManager
from src.observability.trace import set_trace_id
from src.reliability.dead_letter import DeadLetterRecord, DeadLetterStore
from src.session.manager import SessionManager

logger = logging.getLogger(__name__)

router = APIRouter()

# 这些会在 main.py 的 lifespan 中初始化
orchestrator: Optional[Orchestrator] = None
task_manager: Optional[TaskManager] = None
pool_manager: Optional[ContainerPoolManager] = None
orchestrator_resolver = None  # pluggable orchestrator selector (Round 3)
session_manager = None  # SessionManager for multi-turn sessions
_session_service = None  # SessionService for structured state + events
_dispatcher = None  # unified dispatcher (for internal dispatch endpoint)

# Idempotency-Key -> task_id (in-process; cross-process persistence is future work).
_idempotency_index: dict[str, str] = {}

# Dead-letter store for failed orchestrations.
dead_letters = DeadLetterStore()


def set_deps(orch: Orchestrator, tm: TaskManager, pool: ContainerPoolManager, resolver=None, sess_mgr=None, session_svc=None, dispatcher=None):
    global orchestrator, task_manager, pool_manager, orchestrator_resolver, session_manager, _session_service, _dispatcher
    orchestrator = orch
    task_manager = tm
    pool_manager = pool
    orchestrator_resolver = resolver
    _dispatcher = dispatcher
    session_manager = sess_mgr
    _session_service = session_svc


# Per-tenant concurrency cap: backpressure so one tenant can't saturate the workers.
DEFAULT_TENANT_MAX_CONCURRENT = 4
_tenant_semaphores: dict[str, asyncio.Semaphore] = {}


def _get_tenant_semaphore(
    tenant_id: str, limit: int = DEFAULT_TENANT_MAX_CONCURRENT
) -> asyncio.Semaphore:
    """Return the per-tenant semaphore, creating it lazily on first use."""
    sem = _tenant_semaphores.get(tenant_id)
    if sem is None:
        sem = asyncio.Semaphore(limit)
        _tenant_semaphores[tenant_id] = sem
    return sem


# Live orchestration tasks, for real cancellation (not just a status flip).
_running_orchestrations: dict[str, asyncio.Task] = {}


def register_running(task_id: str, orch_task: asyncio.Task) -> None:
    """Track a running orchestration task so it can be cancelled later."""
    _running_orchestrations[task_id] = orch_task
    orch_task.add_done_callback(lambda _t, tid=task_id: _running_orchestrations.pop(tid, None))


def cancel_running(task_id: str) -> bool:
    """Cancel a running orchestration task. Returns False if none / already done."""
    orch_task = _running_orchestrations.get(task_id)
    if orch_task is None or orch_task.done():
        return False
    orch_task.cancel()
    return True


@router.post("/api/chat", response_model=TaskResponse)
async def chat(req: ChatRequest, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")):
    """接收用户消息，创建并执行任务"""
    # 幂等：同一 Idempotency-Key 复用已有 task，不重复编排
    if idempotency_key:
        existing_id = _idempotency_index.get(idempotency_key)
        if existing_id is not None and task_manager is not None:
            existing = task_manager.get_task(existing_id)
            if existing is not None:
                logger.info("idempotent replay key=%s -> task=%s", idempotency_key, existing_id)
                return TaskResponse(
                    task_id=existing.task_id,
                    status=existing.status,
                    message=existing.result,
                    artifacts=existing.artifacts,
                )

    # session: 复用已有（同 work folder + 对话历史）或新建
    tenant = req.tenant_id or "default"
    sess = session_manager.get_or_create(req.session_id, tenant) if session_manager else None

    # SessionService: 结构化 state + events（与 SessionManager 并存）
    if _session_service and sess:
        await _session_service.get_or_create_with_id(sess.session_id, tenant)
        await _session_service.append_event(sess.session_id, {"type": "user_message", "text": req.message})

    # 创建任务
    task = await task_manager.create_task(
        user_message=req.message,
        tenant_id=tenant,
    )

    # session: 覆盖 work_dir 到 session 目录（同一 session 多轮产出落同一处）
    if sess:
        task.work_dir = sess.work_dir
        task.session_id = sess.session_id

    if idempotency_key:
        _idempotency_index[idempotency_key] = task.task_id
    
    # 订阅事件到 WebSocket 广播
    async def on_event(event: dict):
        await ws_manager.broadcast(task.task_id, event)
    
    task.subscribe(on_event)
    
    # 更新状态为运行中
    await task_manager.update_status(task.task_id, TaskStatus.RUNNING)

    # 为本次请求注入 trace id；后台编排任务会继承该上下文，使全程日志可按 task_id 串联
    set_trace_id(task.task_id)

    # 在后台执行编排
    async def run_orchestration():
        # per-tenant backpressure: cap in-flight orchestrations per tenant
        async with _get_tenant_semaphore(task.tenant_id):
            try:
                # Pluggable orchestrator: use the resolver when wired, else the bare orchestrator.
                backend = orchestrator_resolver if orchestrator_resolver is not None else orchestrator
                result = await backend.execute(
                    task_id=task.task_id,
                    tenant_id=task.tenant_id,
                    user_message=req.message,
                    event_callback=on_event,
                    session=sess,
                )
                if sess and session_manager:
                    session_manager.save(sess)
                await task_manager.complete_task(task.task_id, result)
            except Exception as e:
                logger.error(f"Orchestration failed: {e}", exc_info=True)
                if sess and session_manager:
                    session_manager.save(sess)
                dead_letters.record(DeadLetterRecord(
                    task_id=task.task_id,
                    tenant_id=task.tenant_id,
                    error=str(e),
                    user_message=req.message,
                ))
                await task_manager.fail_task(task.task_id, str(e))
    
    orch_task = asyncio.create_task(run_orchestration())
    register_running(task.task_id, orch_task)
    
    return TaskResponse(
        task_id=task.task_id,
        status=TaskStatus.RUNNING,
        message="任务已创建，正在执行中...",
        session_id=sess.session_id if sess else None,
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
        session_id=task.session_id,
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
            session_id=t.session_id,
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


@router.get("/api/tasks/{task_id}/artifacts/{file_path:path}")
async def read_artifact(task_id: str, file_path: str):
    """读取单个产物文件内容（供前端预览）。

    路径相对 task work_dir。返回原始文本；HTML 文件可由前端 iframe 渲染。
    """
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if not task.work_dir:
        raise HTTPException(404, "Task has no work directory")
    full = (task.work_dir / file_path).resolve()
    # 防路径穿越：必须落在 work_dir 内
    try:
        full.relative_to(task.work_dir.resolve())
    except ValueError:
        raise HTTPException(400, "Invalid file path")
    if not full.exists() or not full.is_file():
        raise HTTPException(404, f"File not found: {file_path}")
    content = full.read_text(encoding="utf-8", errors="replace")
    return {"name": file_path, "content": content, "size": full.stat().st_size}


@router.get("/api/agents")
async def list_agents():
    """列出可用 Agent 类型（含在线状态 + endpoint，从 registry 实时读取）。

    优先读 registry（运行时状态）；如果 registry 不可用，回退到静态 config。
    """
    # 尝试从 registry 读实时数据
    if orchestrator and hasattr(orchestrator, '_dispatcher'):
        dispatcher = orchestrator._dispatcher
        for backend in getattr(dispatcher, '_backends', []):
            if hasattr(backend, 'registry') and backend.registry is not None:
                try:
                    agents = await backend.registry.list_agents(online_only=False)
                    if agents:
                        return agents
                except Exception:
                    pass
    # 回退：静态 config
    return [
        {"id": ac.id, "name": ac.name, "description": ac.description,
         "skills": ac.skills, "status": "unknown", "endpoint": ""}
        for ac in settings.agent_cards
    ]


@router.get("/api/dashboard/config")
async def dashboard_config():
    """返回监控仪表板的配置（标题、刷新间隔等）。"""
    return {
        "enabled": settings.dashboard.enabled,
        "title": settings.dashboard.title,
        "refresh_interval": settings.dashboard.refresh_interval,
    }


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


@router.get("/api/v1/dead-letters")
async def list_dead_letters(limit: int = Query(50)):
    """List recent dead-letter records (failed orchestrations)."""
    return [
        {
            "task_id": r.task_id,
            "tenant_id": r.tenant_id,
            "error": r.error,
            "user_message": r.user_message,
            "timestamp": r.timestamp,
        }
        for r in dead_letters.recent(limit)
    ]


@router.get("/api/sessions")
async def list_sessions(tenant_id: Optional[str] = None, limit: int = Query(50)):
    """List sessions with summary info (newest first)."""
    if _session_service is None:
        raise HTTPException(503, "SessionService not available")
    sessions = await _session_service.list_sessions(tenant_id, limit)
    return [
        {
            "session_id": s.session_id,
            "tenant_id": s.tenant_id,
            "work_dir": s.work_dir,
            "created_at": s.created_at,
            "event_count": len(s.events),
            "last_event_type": (s.events[-1].get("type") if s.events else None),
            "last_event_at": (s.events[-1].get("timestamp") if s.events else None),
        }
        for s in sessions
    ]


@router.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    """Get session metadata + state + events."""
    if _session_service is None:
        raise HTTPException(503, "SessionService not available")
    sess = await _session_service.get_session(session_id)
    if sess is None:
        raise HTTPException(404, f"Session {session_id} not found")
    return {
        "session_id": session_id,
        "tenant_id": sess.tenant_id,
        "work_dir": sess.work_dir,
        "created_at": sess.created_at,
        "state": sess.state,
        "events": sess.events,
    }


@router.get("/api/sessions/{session_id}/events")
async def get_session_events(session_id: str):
    """Get structured state + event log for a session (audit trail)."""
    if _session_service is None:
        raise HTTPException(503, "SessionService not available")
    sess = await _session_service.get_session(session_id)
    if sess is None:
        raise HTTPException(404, f"Session {session_id} not found")
    return {"session_id": session_id, "events": sess.events, "state": sess.state}


@router.get("/api/sessions/{session_id}/state")
async def get_session_state(session_id: str):
    """Get structured state for a session."""
    if _session_service is None:
        raise HTTPException(503, "SessionService not available")
    sess = await _session_service.get_session(session_id)
    if sess is None:
        raise HTTPException(404, f"Session {session_id} not found")
    return {"session_id": session_id, "state": sess.state}


# ── internal dispatch endpoint（给外部 orchestrator 如 eino 用）─────────────
# 外部 orchestrator 通过这个端点让 Swarm 调度一个子任务到指定的 agent_type。
# 走完整的 dispatcher 路径（pool checkout + worker 激活 + 执行 + 归还），
# 产物落到 shared_dir 指定的目录。


class InternalDispatchRequest(BaseModel):
    agent_type: str
    task: str
    shared_dir: Optional[str] = None  # worker 产物目录（宿主路径）
    tenant_id: str = "default"
    session_id: Optional[str] = None  # 用于解析 work_dir 并关联会话
    task_id: Optional[str] = None     # 可选：外部 orchestrator 提供的任务 ID


class InternalSessionEventRequest(BaseModel):
    session_id: str
    event_type: str
    payload: dict = Field(default_factory=dict)
    tenant_id: str = "default"


@router.post("/api/internal/dispatch")
async def internal_dispatch(req: InternalDispatchRequest):
    """内部 dispatch：让 Swarm 的 dispatcher 把任务发给指定 agent_type 的 worker。

    供外部 orchestrator（如 eino）调用——它不直连 worker（worker 处于 warm 未激活态），
    而是走这个端点，享受 pool checkout + 激活 + 执行 + 归还的完整链路。
    """
    if _dispatcher is None:
        raise HTTPException(503, "Dispatcher not available")
    from src.dispatcher.base import DispatchRequest

    tenant_id = req.tenant_id or "default"

    # 解析产物目录：显式 shared_dir 优先；否则按 session_id 解析会话工作目录。
    shared_dir = req.shared_dir
    if not shared_dir and req.session_id and _session_service is not None:
        sess = await _session_service.get_or_create_with_id(req.session_id, tenant_id)
        shared_dir = sess.work_dir

    # 注册一个被跟踪的 Swarm task，让产物与任务 ID 关联。
    task_id = req.task_id
    if task_id is None and task_manager is not None:
        task = await task_manager.create_task(user_message=req.task, tenant_id=tenant_id)
        task_id = task.task_id
        if shared_dir:
            task.work_dir = Path(shared_dir)

    context = {"tenant_id": tenant_id}
    if shared_dir:
        context["shared_dir"] = shared_dir
    if task_id:
        context["task_id"] = task_id
    if req.session_id:
        context["session_id"] = req.session_id

    request = DispatchRequest(
        agent_type=req.agent_type,
        task=req.task,
        context=context,
    )
    try:
        result = await _dispatcher.dispatch(request)
    except Exception as e:
        logger.error("internal dispatch failed: %s", e, exc_info=True)
        raise HTTPException(500, str(e))

    return {
        "success": result.success,
        "output": result.output,
        "error": result.error,
        "artifacts": result.artifacts,
        "task_id": task_id,
    }


@router.post("/api/internal/session-event")
async def internal_session_event(req: InternalSessionEventRequest):
    """外部 orchestrator 向 Swarm session 追加事件。

    让外部编排器（如 eino-agent）也能把 dispatch、progress、complete 等关键步骤
    写入 session 事件流，/dashboard 从而能看到完整调度过程。
    """
    if _session_service is None:
        raise HTTPException(503, "SessionService not available")

    await _session_service.get_or_create_with_id(req.session_id, req.tenant_id)
    event = {"type": req.event_type, **req.payload}
    await _session_service.append_event(req.session_id, event)
    return {"ok": True}


@router.get("/api/v1/metrics")
async def get_metrics_endpoint():
    """Dispatch metrics snapshot (counters, latency, failure rate)."""
    from src.observability.metrics import get_metrics
    return get_metrics().snapshot()


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
                cancel_running(task_id)
                await task_manager.update_status(task_id, TaskStatus.CANCELLED)
                # 显式 emit cancel 事件：WS 广播 + session 事件流
                cancel_event = {
                    "type": "cancelled",
                    "task_id": task_id,
                    "data": {"reason": "client_requested"},
                }
                await ws_manager.broadcast(task_id, cancel_event)
                t = task_manager.get_task(task_id)
                sid = getattr(t, "session_id", None) if t else None
                if _session_service and sid:
                    try:
                        await _session_service.append_event(sid, {"type": "cancelled", "task_id": task_id})
                    except Exception:
                        logger.warning("Failed to append cancel event to session %s", sid, exc_info=True)
                break
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(websocket, task_id)
