"""Cancel-event propagation: cancelling a task emits a 'cancelled' event into
both the WebSocket stream and the session's event log.

We exercise the cancel code path via a real WebSocket connection against a
minimal app, then assert the session_v2 store received a cancelled event.
"""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import sys
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("openai", MagicMock())


@pytest.mark.asyncio
async def test_cancel_emits_session_event(tmp_path):
    """A cancelled task records a 'cancelled' event in SessionService."""
    from src.api.routes import router, set_deps, register_running
    from src.session.service import SessionService
    from src.api.models import TaskStatus

    work_base = str(tmp_path / "shared")
    svc = SessionService(tmp_path / "swarm.db", work_base)
    # pre-create the session the task will reference
    await svc.get_or_create_with_id("sess-cancel-1", "default")

    from src.task_manager.manager import Task
    task = Task(task_id="t-cancel", tenant_id="default", user_message="x", status=TaskStatus.RUNNING)
    task.session_id = "sess-cancel-1"
    task.subscribe = lambda cb: None  # type: ignore[method-assign]

    tm = MagicMock()
    tm.get_task = MagicMock(return_value=task)
    tm.update_status = AsyncMock()
    tm.create_task = AsyncMock(return_value=task)
    tm.list_tasks = MagicMock(return_value=[])
    tm.create_artifact_zip = AsyncMock(return_value=None)
    tm.get_artifacts_dir = MagicMock(return_value=None)

    # a long-running orchestration that we can cancel
    hang = asyncio.Event()

    async def fake_execute(**kw):
        try:
            await asyncio.wait_for(hang.wait(), timeout=30)
        except asyncio.CancelledError:
            raise
        return "done"

    orch = MagicMock()
    orch.execute = fake_execute

    pool = MagicMock()
    pool.get_status.return_value = {"total": 0, "idle": 0}

    set_deps(orch, tm, pool, session_svc=svc)

    @asynccontextmanager
    async def lifespan(app):
        yield

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    # register a background task that the WS cancel can target
    bg = asyncio.get_event_loop().create_task(fake_execute())
    register_running("t-cancel", bg)

    # open WS and send cancel
    with client.websocket_connect("/ws/tasks/t-cancel") as ws:
        ws.send_json({"action": "cancel"})
        # the route breaks after handling cancel; give it a moment
        await asyncio.sleep(0.1)

    bg.cancel()
    try:
        await bg
    except asyncio.CancelledError:
        pass

    # the session event log should now contain a cancelled event
    sess = await svc.get_session("sess-cancel-1")
    assert sess is not None
    types = [e.get("type") for e in sess.events]
    assert "cancelled" in types
