"""Tests for the enriched direct-chat path on POST /api/v1/agents/{id}/invoke.

Covers:
- With session_id: returns a TaskResponse-shaped dict, creates a tracked task,
  routes through the dispatcher with agent_id direct-selection, and forwards
  progress to the WebSocket.
- Without session_id: original thin invoke behavior preserved (AgentResult).
- Direct-chat 503s cleanly when optional deps aren't wired.
- 404 when the agent isn't registered.
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.dispatcher.base import DispatchResult, DispatchTarget, TargetKind
from src.gateway.routes import router, set_deps


# ── fakes ─────────────────────────────────────────────────────────────────────


def _make_task(real_work_base: Path):
    """A minimal Task object compatible with what the route touches."""
    from src.task_manager.manager import Task
    from src.api.models import TaskStatus

    t = Task(
        task_id="dir-task-1",
        tenant_id="default",
        user_message="hi",
        status=TaskStatus.CREATED,
    )
    t.work_dir = real_work_base
    t.subscribe = lambda cb: None  # type: ignore[method-assign]
    return t


@pytest.fixture
def app_with_direct_chat(tmp_path):
    """Build a gateway app with the direct-chat deps wired (mocks where useful)."""
    from src.task_manager.manager import TaskManager

    work_base = tmp_path / "shared"
    work_base.mkdir()
    (work_base / "frontend").mkdir()
    task_mgr = TaskManager(shared_output_base=str(work_base), store=None)

    # session manager: real one, pointed at tmp
    from src.session.manager import SessionManager

    sess_mgr = SessionManager(base=str(work_base), store=None)

    # registry: returns one a2a agent with skill "frontend-engineer"
    registry = MagicMock()
    registry.get_agent = AsyncMock(
        return_value={
            "id": "agent-abc",
            "name": "Frontend Engineer",
            "endpoint": "http://localhost:9001",
            "protocol": "a2a",
            "skills": ["frontend-engineer"],
        }
    )

    # dispatcher: records the request, returns a successful result
    dispatcher = MagicMock()
    captured = {}

    async def fake_dispatch(request):
        captured["request"] = request
        return DispatchResult(
            success=True,
            output="agent says hello",
            target=DispatchTarget(kind=TargetKind.EXTERNAL, agent_type="frontend-engineer", agent_id="agent-abc"),
        )

    dispatcher.dispatch = fake_dispatch

    # adapter manager: has the agent (thin-invoke path still works)
    adapter_manager = MagicMock()
    adapter = MagicMock()
    adapter.invoke = AsyncMock(
        return_value=MagicMock(success=True, output="thin-ok", error=None)
    )
    adapter_manager.get = MagicMock(return_value=adapter)

    set_deps(
        registry, adapter_manager,
        task_manager=task_mgr, session_manager=sess_mgr, dispatcher=dispatcher,
    )

    @asynccontextmanager
    async def lifespan(app):
        yield

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    return app, captured, task_mgr


# ── direct-chat path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_direct_chat_creates_task_and_routes_by_agent_id(app_with_direct_chat):
    app, captured, task_mgr = app_with_direct_chat
    client = TestClient(app)

    resp = client.post(
        "/api/v1/agents/agent-abc/invoke",
        json={"task": "build a button", "session_id": "sess-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running"
    assert body["session_id"] == "sess-1"
    assert body["agent_id"] == "agent-abc"
    task_id = body["task_id"]

    # the background dispatch runs asynchronously; give it a moment
    for _ in range(50):
        await asyncio.sleep(0.02)
        if task_mgr.get_task(task_id) and task_mgr.get_task(task_id).status.value in ("completed", "failed"):
            break

    # dispatcher was called with direct-selection (agent_id set, agent_type from skill)
    req = captured["request"]
    assert req.agent_id == "agent-abc"
    assert req.agent_type == "frontend-engineer"
    assert req.task == "build a button"
    assert req.on_progress is not None  # streaming callback wired

    # task got completed with the agent output
    t = task_mgr.get_task(task_id)
    assert t is not None
    assert t.status.value == "completed"
    assert t.result == "agent says hello"


@pytest.mark.asyncio
async def test_direct_chat_404_when_agent_unknown(app_with_direct_chat):
    app, captured, task_mgr = app_with_direct_chat
    # override registry to return None for an unknown agent
    from src.gateway import routes as gr

    gr._registry.get_agent = AsyncMock(return_value=None)
    client = TestClient(app)

    resp = client.post(
        "/api/v1/agents/ghost/invoke",
        json={"task": "x", "session_id": "s"},
    )
    assert resp.status_code == 404


def test_direct_chat_503_when_deps_not_wired():
    """When task_manager/session/dispatcher aren't wired, direct-chat must 503
    (not 500) and the thin path must still work."""
    registry = MagicMock()
    registry.get_agent = AsyncMock(return_value={"id": "a", "skills": ["x"], "name": "n"})
    adapter_manager = MagicMock()
    adapter = MagicMock()
    adapter.invoke = AsyncMock(return_value=MagicMock(success=True, output="ok", error=None))
    adapter_manager.get = MagicMock(return_value=adapter)

    # wire WITHOUT the optional direct-chat deps
    set_deps(registry, adapter_manager)

    @asynccontextmanager
    async def lifespan(app):
        yield

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    client = TestClient(app)

    # direct-chat (session_id present) → 503
    resp = client.post("/api/v1/agents/a/invoke", json={"task": "x", "session_id": "s"})
    assert resp.status_code == 503

    # thin invoke (no session_id) → still works
    resp2 = client.post("/api/v1/agents/a/invoke", json={"task": "x"})
    assert resp2.status_code == 200
    assert resp2.json()["success"] is True


# ── thin-invoke regression ────────────────────────────────────────────────────


def test_thin_invoke_still_returns_agent_result(app_with_direct_chat):
    """Without session_id, the original AgentResult shape is returned."""
    app, _, _ = app_with_direct_chat
    client = TestClient(app)

    resp = client.post("/api/v1/agents/agent-abc/invoke", json={"task": "hello"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] == "agent-abc"
    assert body["success"] is True
    assert body["result"] == "thin-ok"
