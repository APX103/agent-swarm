"""Lightweight coverage tests for public functions/classes without direct tests."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.agents import worker as worker_module
from src.api import routes as api_routes
from src.api.websocket import WSConnection, WSConnectionManager
from src.config import load_settings
from src.main import create_app
from src.observability.metrics import get_metrics
from src.registry.models import AgentInfo, AgentRegistration
from src.swarm_sdk.client import AgentClient, start_heartbeat_loop


# ── helpers ──────────────────────────────────────────────────────────────────


class _DummyWebSocket:
    def __init__(self):
        self.accepted = False
        self.sent: list[dict] = []

    async def accept(self):
        self.accepted = True

    async def send_json(self, data: dict):
        self.sent.append(data)


@asynccontextmanager
async def _dummy_lifespan(app: FastAPI):
    yield


def _make_main_app() -> FastAPI:
    app = create_app(lifespan=_dummy_lifespan)
    return app


def _mock_task(task_id: str = "t123", status: Any = None, result: str = "", artifacts=None):
    task = MagicMock()
    task.task_id = task_id
    task.status = status or MagicMock(value="completed")
    task.result = result
    task.artifacts = artifacts or []
    task.work_dir = None
    task.session_id = None
    task.tenant_id = "default"
    return task


# ── main app / api routes ────────────────────────────────────────────────────


def test_create_app_returns_fastapi_app():
    app = create_app(lifespan=_dummy_lifespan)
    assert isinstance(app, FastAPI)


def test_dashboard_config_endpoint():
    app = _make_main_app()
    api_routes.set_deps(MagicMock(), MagicMock(), MagicMock())
    with TestClient(app) as client:
        resp = client.get("/api/dashboard/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "enabled" in data and "title" in data


@pytest.mark.asyncio
async def test_list_dead_letters_endpoint():
    app = _make_main_app()
    await api_routes.dead_letters.clear()
    await api_routes.dead_letters.record(
        api_routes.DeadLetterRecord(
            task_id="t1", tenant_id="default", error="boom", user_message="hi"
        )
    )
    api_routes.set_deps(MagicMock(), MagicMock(), MagicMock())
    with TestClient(app) as client:
        resp = client.get("/api/v1/dead-letters")
    assert resp.status_code == 200
    assert resp.json()[0]["task_id"] == "t1"


def test_get_metrics_endpoint():
    app = _make_main_app()
    api_routes.set_deps(MagicMock(), MagicMock(), MagicMock())
    with TestClient(app) as client:
        resp = client.get("/api/v1/metrics")
    assert resp.status_code == 200
    assert "dispatch_total" in resp.json()


def test_internal_session_event_endpoint(tmp_path):
    app = _make_main_app()
    svc = AsyncMock()
    svc.get_or_create_with_id = AsyncMock()
    svc.append_event = AsyncMock()
    api_routes.set_deps(
        MagicMock(), MagicMock(), MagicMock(), session_svc=svc, dispatcher=MagicMock()
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/internal/session-event",
            json={
                "session_id": "s1",
                "event_type": "test_event",
                "payload": {"x": 1},
                "tenant_id": "default",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    svc.append_event.assert_awaited_once()


def test_internal_dispatch_endpoint(tmp_path):
    app = _make_main_app()
    dispatcher = AsyncMock()
    dispatcher.dispatch = AsyncMock(
        return_value=MagicMock(success=True, output="ok", error=None, artifacts=[])
    )
    tm = MagicMock()
    tm.create_task = AsyncMock(return_value=_mock_task(task_id="dt1"))
    svc = AsyncMock()
    svc.get_or_create_with_id = AsyncMock(
        return_value=MagicMock(work_dir=str(tmp_path))
    )
    api_routes.set_deps(
        MagicMock(), tm, MagicMock(), session_svc=svc, dispatcher=dispatcher
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/internal/dispatch",
            json={"agent_type": "backend-engineer", "task": "write tests"},
        )
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_session_endpoints(tmp_path):
    app = _make_main_app()
    from src.session.models import Session

    svc = AsyncMock()
    svc.list_sessions = AsyncMock(
        return_value=[
            Session(
                session_id="s1",
                tenant_id="default",
                work_dir=str(tmp_path),
                state={},
                events=[{"type": "user_message", "timestamp": 1.0}],
                created_at=1.0,
            )
        ]
    )
    svc.get_session = AsyncMock(
        return_value=Session(
            session_id="s1",
            tenant_id="default",
            work_dir=str(tmp_path),
            state={"x": 1},
            events=[{"type": "user_message", "timestamp": 1.0}],
            created_at=1.0,
        )
    )
    api_routes.set_deps(
        MagicMock(), MagicMock(), MagicMock(), session_svc=svc, dispatcher=MagicMock()
    )
    with TestClient(app) as client:
        resp_list = client.get("/api/sessions")
        resp_events = client.get("/api/sessions/s1/events")
        resp_state = client.get("/api/sessions/s1/state")
    assert resp_list.status_code == 200
    assert resp_events.status_code == 200
    assert resp_state.status_code == 200
    assert resp_state.json()["state"]["x"] == 1


def test_download_artifacts_endpoint(tmp_path):
    app = _make_main_app()
    work_dir = tmp_path / "tasks" / "t1"
    work_dir.mkdir(parents=True)
    (work_dir / "file.txt").write_text("hello")
    zip_path = work_dir / "t1_artifacts.zip"
    import shutil

    shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=work_dir)

    tm = MagicMock()
    tm.create_artifact_zip = AsyncMock(return_value=str(zip_path))
    api_routes.set_deps(MagicMock(), tm, MagicMock())
    with TestClient(app) as client:
        resp = client.get("/api/tasks/t1/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"


# ── WebSocket connection manager ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_manager_connect_broadcast_disconnect():
    mgr = WSConnectionManager()
    ws1 = _DummyWebSocket()
    ws2 = _DummyWebSocket()
    await mgr.connect(ws1, "task-1")
    await mgr.connect(ws2, "task-1")
    assert await mgr.connection_count("task-1") == 2
    await mgr.broadcast("task-1", {"type": "ping"})
    assert len(ws1.sent) == 1
    assert len(ws2.sent) == 1
    await mgr.disconnect(ws1, "task-1")
    assert await mgr.connection_count("task-1") == 1


@pytest.mark.asyncio
async def test_ws_manager_broadcast_removes_dead_connection():
    mgr = WSConnectionManager()
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock(side_effect=RuntimeError("dead"))
    await mgr.connect(ws, "task-x")
    await mgr.broadcast("task-x", {"type": "ping"})
    assert await mgr.connection_count("task-x") == 0


# ── worker endpoints ─────────────────────────────────────────────────────────


def test_worker_well_known_agent():
    with TestClient(worker_module.app) as client:
        resp = client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    assert "name" in resp.json()


def test_worker_a2a_endpoint_invalid_json():
    with TestClient(worker_module.app) as client:
        resp = client.post("/", data="not-json", headers={"Content-Type": "application/json"})
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == -32700


def test_worker_a2a_endpoint_send_message_blocking(monkeypatch):
    monkeypatch.setattr(worker_module, "LLM_API_KEY", "fake-key")
    with patch.object(worker_module, "run_agent_loop", new_callable=AsyncMock) as mock_loop:
        mock_loop.return_value = "done"
        with TestClient(worker_module.app) as client:
            resp = client.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "message/send",
                    "params": {
                        "message": {"parts": [{"kind": "text", "text": "hi"}]},
                        "configuration": {"blocking": True},
                    },
                },
            )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["status"]["state"] == "completed"


def test_worker_a2a_endpoint_tasks_list_and_cancel(monkeypatch):
    monkeypatch.setattr(worker_module, "LLM_API_KEY", "fake-key")

    async def _slow_loop(*args, **kwargs):
        await asyncio.sleep(0.1)
        return "done"

    # First create a non-blocking task.
    with patch.object(worker_module, "run_agent_loop", new_callable=AsyncMock) as mock_loop:
        mock_loop.side_effect = _slow_loop
        with TestClient(worker_module.app) as client:
            send_resp = client.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "message/send",
                    "params": {
                        "message": {"parts": [{"kind": "text", "text": "hi"}]},
                        "configuration": {"blocking": False},
                    },
                },
            )
            task_id = send_resp.json()["result"]["id"]
            list_resp = client.post(
                "/",
                json={"jsonrpc": "2.0", "id": 2, "method": "tasks/list", "params": {}},
            )
            cancel_resp = client.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tasks/cancel",
                    "params": {"id": task_id},
                },
            )
    assert list_resp.status_code == 200
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["result"]["status"] == "canceled"


def test_reload_config_reads_existing_config(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text('{"agent_role": "backend-engineer", "task_id": "tid"}')
    monkeypatch.setattr(worker_module, "CONFIG_FILE", str(config_file))
    worker_module.reload_config()
    assert worker_module.AGENT_ROLE == "backend-engineer"
    assert worker_module.TASK_ID == "tid"


# ── SDK / config / registry / metrics ────────────────────────────────────────


def test_agent_client_context_manager():
    async def _run():
        async with AgentClient("http://localhost:9000") as client:
            assert client._gateway_url == "http://localhost:9000"

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_start_heartbeat_loop():
    client = MagicMock(spec=AgentClient)
    client.heartbeat = AsyncMock(return_value=True)
    task = asyncio.create_task(start_heartbeat_loop(client, "a1", interval=0.01))
    await asyncio.sleep(0.03)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert client.heartbeat.await_count >= 1


def test_load_settings_returns_settings():
    settings = load_settings()
    assert settings.server.port is not None
    assert settings.llm.default_model is not None


def test_get_metrics_singleton():
    m1 = get_metrics()
    m2 = get_metrics()
    assert m1 is m2


def test_registry_models_instantiate():
    reg = AgentRegistration(name="x", endpoint="http://e", protocol="openai")
    info = AgentInfo(
        id="a1",
        name="x",
        endpoint="http://e",
        protocol="openai",
        skills=[],
        status="online",
    )
    assert reg.name == "x"
    assert info.id == "a1"


# ── misc public imports / shapes ─────────────────────────────────────────────


def test_dispatcher_backend_protocol_import():
    from src.dispatcher.backends import DispatchBackend

    assert DispatchBackend is not None
