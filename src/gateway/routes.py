"""Gateway API routes for external agent registration."""
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agents")

# Module-level dependencies, set during app lifespan
_registry = None
_adapter_manager = None


def set_deps(registry, adapter_manager):
    """Set gateway dependencies (called from main.py lifespan)."""
    global _registry, _adapter_manager
    _registry = registry
    _adapter_manager = adapter_manager


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class AgentRegistration(BaseModel):
    name: str
    endpoint: str
    protocol: str = "http"
    skills: list[str] = []
    heartbeat_interval: int = 10


class InvokeRequest(BaseModel):
    task: str
    context: dict[str, Any] = {}


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


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/register", response_model=RegistrationResponse)
async def register_agent(body: AgentRegistration):
    """Register a new external agent."""
    if _registry is None:
        raise HTTPException(status_code=503, detail="Registry not available")
    try:
        agent_id = await _registry.register(
            name=body.name,
            endpoint=body.endpoint,
            protocol=body.protocol,
            skills=body.skills,
            heartbeat_interval=body.heartbeat_interval,
        )
        return RegistrationResponse(
            agent_id=agent_id,
            heartbeat_interval=body.heartbeat_interval,
            status="registered",
        )
    except Exception as e:
        logger.exception("Failed to register agent")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{agent_id}/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(agent_id: str):
    """Receive heartbeat from an agent."""
    if _registry is None:
        raise HTTPException(status_code=503, detail="Registry not available")
    try:
        interval = await _registry.heartbeat(agent_id)
        return HeartbeatResponse(
            status="ok",
            next_heartbeat_in=interval,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    except Exception as e:
        logger.exception("Heartbeat failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{agent_id}/deregister", response_model=DeregisterResponse)
async def deregister_agent(agent_id: str):
    """Remove an agent from the registry."""
    if _registry is None:
        raise HTTPException(status_code=503, detail="Registry not available")
    try:
        await _registry.deregister(agent_id)
        return DeregisterResponse(status="deregistered")
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    except Exception as e:
        logger.exception("Deregister failed")
        raise HTTPException(status_code=500, detail=str(e))


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


@router.post("/{agent_id}/invoke", response_model=AgentResult)
async def invoke_agent(agent_id: str, body: InvokeRequest):
    """Invoke a registered agent with a task."""
    if _registry is None:
        raise HTTPException(status_code=503, detail="Registry not available")
    if _adapter_manager is None:
        raise HTTPException(status_code=503, detail="Adapter manager not available")
    try:
        adapter = _adapter_manager.get(agent_id)
        if adapter is None:
            raise HTTPException(status_code=404, detail=f"No adapter for agent {agent_id}")
        result = await adapter.invoke(task=body.task, context=body.context)
        return AgentResult(
            agent_id=agent_id,
            success=result.success,
            result=result.output,
            error=result.error,
        )
    except (KeyError, HTTPException):
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    except Exception as e:
        logger.exception("Invoke failed")
        raise HTTPException(status_code=500, detail=str(e))
