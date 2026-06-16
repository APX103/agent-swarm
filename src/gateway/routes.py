"""Gateway API routes for external agent registration."""
import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from src.adapters.adapter_manager import create_adapter
from src.api.models import TaskStatus
from src.api.websocket import ws_manager
from src.dispatcher.base import DispatchRequest
from src.resilience.circuit_breaker import CircuitBreaker, CircuitOpenError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agents")

# Module-level dependencies, set during app lifespan
_registry = None
_adapter_manager = None

# Optional deps for the enriched direct-chat path (set from main.py). When absent,
# /invoke falls back to the original thin adapter.invoke (no session/task/streaming).
_task_manager = None
_session_manager = None
_session_service = None
_dispatcher = None

# Default heartbeat interval (seconds) advertised to agents. The real
# AgentRegistry does not store a per-agent interval, so the gateway surfaces
# this default rather than leaking the registry's bool return value.
DEFAULT_HEARTBEAT_INTERVAL = 10

# Per-agent circuit breakers guarding /invoke against failing external agents.
_invoke_breakers: dict[str, CircuitBreaker] = {}


def _get_invoke_breaker(agent_id: str) -> CircuitBreaker:
    """Return the per-agent circuit breaker, creating it lazily."""
    cb = _invoke_breakers.get(agent_id)
    if cb is None:
        cb = CircuitBreaker()
        _invoke_breakers[agent_id] = cb
    return cb


def set_deps(registry, adapter_manager, task_manager=None, session_manager=None, session_service=None, dispatcher=None):
    """Set gateway dependencies (called from main.py lifespan).

    The last three are optional and enable the enriched direct-chat path on
    POST /{agent_id}/invoke (session + streaming + task tracking). When omitted,
    /invoke keeps its original thin behavior.
    """
    global _registry, _adapter_manager, _task_manager, _session_manager, _session_service, _dispatcher
    _registry = registry
    _adapter_manager = adapter_manager
    _task_manager = task_manager
    _session_manager = session_manager
    _session_service = session_service
    _dispatcher = dispatcher
    _invoke_breakers.clear()  # reset breaker state on (re)wiring


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class AgentRegistration(BaseModel):
    model_config = ConfigDict(extra="allow")  # accept adapter-specific fields (base_url, command, ...)
    name: str
    endpoint: str
    protocol: str = "http"
    skills: list[str] = []
    heartbeat_interval: int = 10


class InvokeRequest(BaseModel):
    task: str
    context: dict[str, Any] = {}
    # When set, the invoke runs as a tracked task in this session (direct-chat):
    # session work folder is reused, progress streams over WebSocket, and the
    # response is a TaskResponse (same shape as /api/chat) instead of AgentResult.
    session_id: Optional[str] = None


class RegistrationResponse(BaseModel):
    agent_id: str
    heartbeat_interval: int
    status: str


class HeartbeatResponse(BaseModel):
    status: str
    next_heartbeat_in: int


class DeregisterResponse(BaseModel):
    status: str


class AgentResult(BaseModel):
    agent_id: str
    success: bool
    result: Optional[Any] = None
    error: Optional[str] = None


# Protocols backed by a real adapter (auto-provisioned on register).
ADAPTER_PROTOCOLS: tuple[str, ...] = ("openai", "cli", "mcp", "a2a")
# All protocols accepted at registration time (adapter protocols + generic http).
KNOWN_PROTOCOLS: tuple[str, ...] = ADAPTER_PROTOCOLS + ("http",)


def _build_adapter_info(body: AgentRegistration) -> dict:
    """Translate a registration body into an adapter info dict.

    Protocol-specific connection config may be supplied as extra fields
    (base_url/model/api_key, command/args, server_url, ...). When omitted, the
    registration ``endpoint`` is used as the connection target.
    """
    extra = body.model_dump(exclude={"name", "endpoint", "protocol", "skills", "heartbeat_interval"})
    info: dict = {"protocol": body.protocol, **extra}
    if body.protocol in ("openai", "a2a"):
        info.setdefault("base_url", body.endpoint)
    elif body.protocol == "cli":
        info.setdefault("command", body.endpoint)
        info.setdefault("args", [])
    elif body.protocol == "mcp":
        info.setdefault("server_url", body.endpoint)
    return info


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/register", response_model=RegistrationResponse)
async def register_agent(body: AgentRegistration):
    """Register a new external agent.

    For adapter protocols (openai/cli/mcp/a2a) an adapter is auto-provisioned so the
    agent is immediately invocable. Validation happens before any state is written: a
    bad protocol or adapter config is rejected (400) with nothing persisted.
    """
    if _registry is None:
        raise HTTPException(status_code=503, detail="Registry not available")

    # 1. validate protocol
    if body.protocol not in KNOWN_PROTOCOLS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown protocol '{body.protocol}'. Supported: {', '.join(KNOWN_PROTOCOLS)}",
        )

    # 2. for adapter protocols, validate+build the adapter BEFORE persisting, so a bad
    #    config is rejected cleanly (400) with nothing to roll back.
    adapter = None
    if body.protocol in ADAPTER_PROTOCOLS:
        if _adapter_manager is None:
            raise HTTPException(status_code=503, detail="Adapter manager not available")
        try:
            adapter = create_adapter(_build_adapter_info(body))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid adapter config: {e}")

    # 3. persist the agent
    agent_data = {
        "name": body.name,
        "endpoint": body.endpoint,
        "protocol": body.protocol,
        "skills": body.skills,
    }
    try:
        agent_id = await _registry.register(agent_data)
    except Exception as e:
        logger.exception("Failed to register agent")
        raise HTTPException(status_code=500, detail=str(e))

    # 4. register the already-validated adapter (roll back on unexpected failure)
    if adapter is not None:
        try:
            _adapter_manager.register(agent_id, adapter)
        except Exception:
            logger.exception("Failed to register adapter for %s; rolling back", agent_id)
            try:
                await _registry.deregister(agent_id)
            except Exception:
                logger.warning("Rollback deregister failed for %s", agent_id, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to provision adapter")

    logger.info("Registered agent %s (%s, protocol=%s)", agent_id, body.name, body.protocol)
    return RegistrationResponse(
        agent_id=agent_id,
        heartbeat_interval=body.heartbeat_interval,
        status="registered",
    )


@router.post("/{agent_id}/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(agent_id: str):
    """Receive heartbeat from an agent."""
    if _registry is None:
        raise HTTPException(status_code=503, detail="Registry not available")
    # AgentRegistry.heartbeat(agent_id) -> bool (True=renewed, False=unknown).
    try:
        renewed = await _registry.heartbeat(agent_id)
    except Exception as e:
        logger.exception("Heartbeat failed")
        raise HTTPException(status_code=500, detail=str(e))
    if not renewed:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    return HeartbeatResponse(status="ok", next_heartbeat_in=DEFAULT_HEARTBEAT_INTERVAL)


@router.post("/{agent_id}/deregister", response_model=DeregisterResponse)
async def deregister_agent(agent_id: str):
    """Remove an agent from the registry."""
    if _registry is None:
        raise HTTPException(status_code=503, detail="Registry not available")
    # AgentRegistry.deregister is a safe no-op for unknown ids, so check existence
    # explicitly to distinguish 404 from idempotent removal.
    try:
        existing = await _registry.get_agent(agent_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
        await _registry.deregister(agent_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Deregister failed")
        raise HTTPException(status_code=500, detail=str(e))
    return DeregisterResponse(status="deregistered")


@router.get("", response_model=list)
async def list_agents(skill: Optional[str] = Query(None)):
    """List all registered agents, optionally filtered by skill."""
    if _registry is None:
        raise HTTPException(status_code=503, detail="Registry not available")
    try:
        if skill:
            return await _registry.find_by_skill(skill)
        return await _registry.list_agents()
    except Exception as e:
        logger.exception("Failed to list agents")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{agent_id}/invoke")
async def invoke_agent(agent_id: str, body: InvokeRequest):
    """Invoke a registered agent with a task.

    Two modes:
    - **Direct-chat** (body.session_id present): runs as a tracked task inside
      the session — reuses the session work folder, streams progress over
      WebSocket (ws/tasks/{task_id}), and returns a TaskResponse-shaped dict
      (same as /api/chat). Requires the optional deps wired via set_deps.
    - **Thin invoke** (no session_id): the original behavior — calls the adapter
      directly through a per-agent circuit breaker, returns AgentResult.
    """
    if _registry is None:
        raise HTTPException(status_code=503, detail="Registry not available")

    if body.session_id is not None:
        return await _invoke_direct_chat(agent_id, body)

    # ── thin invoke (original path) ─────────────────────────────────────────
    if _adapter_manager is None:
        raise HTTPException(status_code=503, detail="Adapter manager not available")
    adapter = _adapter_manager.get(agent_id)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"No adapter for agent {agent_id}")

    breaker = _get_invoke_breaker(agent_id)
    try:
        result = await breaker.call(adapter.invoke, body.task, body.context)
    except CircuitOpenError as e:
        logger.warning("Invoke rejected (circuit open) for agent %s: %s", agent_id, e)
        raise HTTPException(status_code=503, detail=f"Agent {agent_id} circuit open: {e}")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Invoke failed for agent %s", agent_id)
        raise HTTPException(status_code=500, detail=str(e))

    return AgentResult(
        agent_id=agent_id,
        success=result.success,
        result=result.output,
        error=result.error,
    )


async def _invoke_direct_chat(agent_id: str, body: InvokeRequest) -> dict:
    """Enriched direct-chat: tracked task + session + streaming + dispatcher routing.

    Resolves the agent's skill (agent_type) from the registry so the Dispatcher
    can route by id directly, and forwards progress to the WebSocket.
    """
    if _task_manager is None or _session_service is None or _dispatcher is None:
        raise HTTPException(
            status_code=503,
            detail="Direct-chat not available (task_manager/session_service/dispatcher not wired)",
        )

    # resolve agent record (need its skill as agent_type + existence check)
    try:
        agent = await _registry.get_agent(agent_id)
    except Exception:
        logger.exception("get_agent failed for direct-chat %s", agent_id)
        raise HTTPException(status_code=500, detail="Registry lookup failed")
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    agent_type = (agent.get("skills") or [None])[0] or agent.get("name") or agent_id

    # session: reuse work folder + persist events to SessionService (same backend as /api/sessions)
    sess = await _session_service.get_or_create_with_id(body.session_id, tenant_id="default")

    # tracked task
    task = await _task_manager.create_task(user_message=body.task, tenant_id="default")
    task.session_id = body.session_id
    task.work_dir = Path(sess.work_dir)

    async def on_event(event: dict):
        await ws_manager.broadcast(task.task_id, event)

    task.subscribe(on_event)
    await _task_manager.update_status(task.task_id, TaskStatus.RUNNING)

    # forward worker progress snapshots to the WS as agent_progress events
    async def on_progress(snap: dict):
        await ws_manager.broadcast(
            task.task_id,
            {"type": "agent_progress", "task_id": task.task_id, "agent": agent_id, "data": snap},
        )

    ctx = {"task_id": task.task_id, "tenant_id": "default", "shared_dir": sess.work_dir}
    request = DispatchRequest(
        agent_type=agent_type,
        task=body.task,
        context=ctx,
        on_progress=on_progress,
        agent_id=agent_id,  # direct selection — bypass skill matching
    )

    async def run_direct():
        try:
            await _session_service.append_event(body.session_id, {
                "type": "agent_dispatched",
                "agent_type": agent_type,
                "task": body.task,
            })
            result = await _dispatcher.dispatch(request)
            if result.success:
                await _task_manager.complete_task(task.task_id, result.output or "")
                await _session_service.append_event(body.session_id, {
                    "type": "agent_completed",
                    "agent_type": agent_type,
                    "success": True,
                })
            else:
                await _task_manager.fail_task(task.task_id, result.error or "direct-chat failed")
                await _session_service.append_event(body.session_id, {
                    "type": "agent_completed",
                    "agent_type": agent_type,
                    "success": False,
                    "error": result.error,
                })
        except Exception as e:
            logger.error("Direct-chat dispatch failed: %s", e, exc_info=True)
            await _task_manager.fail_task(task.task_id, str(e))
            await _session_service.append_event(body.session_id, {
                "type": "agent_completed",
                "agent_type": agent_type,
                "success": False,
                "error": str(e),
            })

    asyncio.create_task(run_direct())

    return {
        "task_id": task.task_id,
        "status": TaskStatus.RUNNING.value,
        "message": "Direct-chat task created, streaming over WebSocket.",
        "artifacts": [],
        "session_id": sess.session_id,
        "agent_id": agent_id,
    }
