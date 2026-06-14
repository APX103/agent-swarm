"""测试 API 路由层"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from contextlib import asynccontextmanager

# 在导入前 mock 掉 docker 和 openai
import sys
sys.modules.setdefault('docker', MagicMock())
sys.modules.setdefault('openai', MagicMock())


def _create_test_app(mock_orchestrator, mock_task_manager, mock_pool):
    """创建测试用 FastAPI app，跳过真实 lifespan"""
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from src.api.routes import router, set_deps

    set_deps(mock_orchestrator, mock_task_manager, mock_pool)

    @asynccontextmanager
    async def empty_lifespan(app):
        yield

    app = FastAPI(lifespan=empty_lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    pool.get_status.return_value = {"total": 5, "idle": 3}
    pool.checkout = AsyncMock(return_value=MagicMock(
        container_id="c1", container_name="swarm-worker-0", port=9001
    ))
    pool.return_container = AsyncMock()
    return pool


@pytest.fixture
def mock_task_manager():
    from src.api.models import TaskStatus
    tm = MagicMock()
    tm.create_task = AsyncMock(return_value=MagicMock(
        task_id="test-123",
        tenant_id="default",
        status=TaskStatus.CREATED,
        result=None,
        artifacts=[],
        work_dir=None,
        subscribe=lambda cb: None,
    ))
    tm.update_status = AsyncMock()
    tm.complete_task = AsyncMock()
    tm.fail_task = AsyncMock()
    tm.get_task = MagicMock(return_value=MagicMock(
        task_id="test-123",
        status=TaskStatus.COMPLETED,
        result="done",
        artifacts=["frontend/index.html"],
        work_dir=None,
    ))
    tm.list_tasks = MagicMock(return_value=[])
    tm.get_artifacts_dir = MagicMock(return_value=None)
    tm.create_artifact_zip = AsyncMock(return_value=None)
    return tm


@pytest.fixture
def mock_orchestrator():
    orch = MagicMock()
    orch.execute = AsyncMock(return_value="Task completed successfully")
    return orch


def test_chat_creates_task(mock_pool, mock_task_manager, mock_orchestrator):
    """POST /api/chat 创建任务并启动编排"""
    app = _create_test_app(mock_orchestrator, mock_task_manager, mock_pool)
    client = TestClient(app, raise_server_exceptions=False)
    
    resp = client.post("/api/chat", json={"message": "写一个网站"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == "test-123"
    assert data["status"] == "running"


def test_get_task(mock_pool, mock_task_manager, mock_orchestrator):
    """GET /api/tasks/{id} 返回任务状态"""
    app = _create_test_app(mock_orchestrator, mock_task_manager, mock_pool)
    client = TestClient(app, raise_server_exceptions=False)
    
    resp = client.get("/api/tasks/test-123")
    if resp.status_code != 200:
        print(f"ERROR BODY: {resp.text}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == "test-123"


def test_get_task_not_found(mock_pool, mock_task_manager, mock_orchestrator):
    """GET 不存在的任务返回 404"""
    app = _create_test_app(mock_orchestrator, mock_task_manager, mock_pool)
    client = TestClient(app, raise_server_exceptions=False)
    
    mock_task_manager.get_task.return_value = None
    resp = client.get("/api/tasks/nonexistent")
    if resp.status_code != 404:
        print(f"ERROR BODY: {resp.text}")
    assert resp.status_code == 404


def test_list_agents(mock_pool, mock_task_manager, mock_orchestrator):
    """GET /api/agents 返回可用 Agent 列表"""
    app = _create_test_app(mock_orchestrator, mock_task_manager, mock_pool)
    client = TestClient(app, raise_server_exceptions=False)
    
    resp = client.get("/api/agents")
    assert resp.status_code == 200
    agents = resp.json()
    assert len(agents) >= 1
    assert any(a["id"] == "frontend-ux-pro" for a in agents)


def test_health(mock_pool, mock_task_manager, mock_orchestrator):
    """GET /api/health 返回健康状态"""
    app = _create_test_app(mock_orchestrator, mock_task_manager, mock_pool)
    client = TestClient(app, raise_server_exceptions=False)
    
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["pool_total"] == 5
    assert data["pool_available"] == 3


def test_chat_uses_resolver_when_set(mock_pool, mock_task_manager, mock_orchestrator):
    """R3.4: when a resolver is wired via set_deps, /api/chat routes through it."""
    from src.api.routes import router, set_deps
    from fastapi import FastAPI

    resolver = MagicMock()
    resolver.execute = AsyncMock(return_value="RESOLVED-OK")
    set_deps(mock_orchestrator, mock_task_manager, mock_pool, resolver=resolver)

    @asynccontextmanager
    async def empty(app):
        yield

    app = FastAPI(lifespan=empty)
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/api/chat", json={"message": "x"})
    assert resp.status_code == 200
    resolver.execute.assert_awaited_once()
    mock_orchestrator.execute.assert_not_awaited()
