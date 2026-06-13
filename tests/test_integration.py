"""集成测试：完整流程测试（不含真实 Docker）
测试 Orchestrator → TaskManager → ContainerPool 的交互，
使用 mock 替代真实的 Docker 和 LLM 调用。
"""
import pytest
import asyncio
import sys
from unittest.mock import MagicMock, AsyncMock, patch
from pathlib import Path

# Mock docker and openai before any imports
sys.modules.setdefault('docker', MagicMock())
sys.modules.setdefault('openai', MagicMock())


@pytest.fixture
def full_system(tmp_path):
    """构建完整的 mock 系统"""
    from src.config import load_settings, AgentCardDef
    from src.task_manager.manager import TaskManager
    from src.container_pool.pool import ContainerPoolManager
    from src.orchestrator.orchestrator import Orchestrator
    from src.api.models import TaskStatus
    
    settings = load_settings()
    
    # TaskManager（真实）
    tm = TaskManager(shared_output_base=str(tmp_path))
    
    # ContainerPool（mock Docker）
    mock_docker = MagicMock()
    call_n = {"n": 0}
    def make_container(*args, **kwargs):
        call_n["n"] += 1
        c = MagicMock()
        c.id = f"cont-{call_n['n']}"
        c.stop = MagicMock()
        c.remove = MagicMock()
        return c
    mock_docker.containers.run = MagicMock(side_effect=make_container)
    mock_docker.containers.get = MagicMock(side_effect=make_container)
    mock_docker.images.get = MagicMock(return_value=MagicMock())
    mock_docker.networks.create = MagicMock()
    
    pool = ContainerPoolManager(settings=settings)
    pool._client = mock_docker
    
    # Orchestrator（mock LLM）
    orch = Orchestrator(
        settings=settings,
        pool_manager=pool,
        task_manager=tm,
    )
    
    return {
        "settings": settings,
        "task_manager": tm,
        "pool": pool,
        "orchestrator": orch,
        "mock_docker": mock_docker,
    }


@pytest.mark.asyncio
async def test_full_lifecycle_create_task(full_system):
    """完整流程：创建任务 → 状态更新 → 完成"""
    tm = full_system["task_manager"]
    from src.api.models import TaskStatus
    
    # 创建任务
    task = await tm.create_task("写一个网站", tenant_id="test-tenant")
    assert task.task_id
    assert task.status == TaskStatus.CREATED
    
    # 更新状态
    await tm.update_status(task.task_id, TaskStatus.RUNNING)
    assert tm.get_task(task.task_id).status == TaskStatus.RUNNING
    
    # 写入产物
    (task.work_dir / "frontend" / "index.html").write_text("<h1>Test</h1>")
    
    # 完成任务
    await tm.complete_task(task.task_id, "网站已完成")
    
    final = tm.get_task(task.task_id)
    assert final.status == TaskStatus.COMPLETED
    assert "frontend/index.html" in final.artifacts
    assert final.completed_at is not None


@pytest.mark.asyncio
async def test_pool_startup_and_checkout(full_system):
    """容器池启动 + checkout + return"""
    pool = full_system["pool"]
    
    # Mock httpx health check（startup 和 checkout 内部都会用 httpx 轮询健康端点）
    import httpx
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_cm = MagicMock()
    mock_cm.get = AsyncMock(return_value=mock_resp)
    mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    with patch.object(httpx, "AsyncClient", return_value=mock_cm):
        await pool.startup()
        status = pool.get_status()
        assert status["total"] == 5
        assert status["idle"] == 5
        
        # Checkout 一个容器
        container = await pool.checkout(
            agent_card_id="frontend-ux-pro",
            task_id="test-task",
            model="glm-4.7",
            base_url="https://open.bigmodel.cn/api/coding/paas/v4",
            api_key="test-key",
        )
        assert container is not None
        assert container.state.value == "busy"
        
        status = pool.get_status()
        assert status["busy"] == 1
        assert status["idle"] == 4
        
        # Return
        await pool.return_container(container.container_id)
        status = pool.get_status()
        assert status["busy"] == 0
        assert status["idle"] == 5
        
        await pool.shutdown()


@pytest.mark.asyncio
async def test_multi_tenant_isolation(full_system):
    """多租户隔离测试"""
    tm = full_system["task_manager"]
    
    t1 = await tm.create_task("任务1", tenant_id="tenant-A")
    t2 = await tm.create_task("任务2", tenant_id="tenant-B")
    
    # 不同租户的工作目录应该隔离
    assert t1.work_dir != t2.work_dir
    assert "tenant-A" in str(t1.work_dir)
    assert "tenant-B" in str(t2.work_dir)
    
    # 列表过滤
    all_tasks = tm.list_tasks()
    assert len(all_tasks) == 2
    
    a_tasks = tm.list_tasks(tenant_id="tenant-A")
    assert len(a_tasks) == 1
    assert a_tasks[0].task_id == t1.task_id


@pytest.mark.asyncio
async def test_orchestrator_simple_flow(full_system):
    """Orchestrator 简单执行流程（mock LLM）"""
    orch = full_system["orchestrator"]
    tm = full_system["task_manager"]
    
    # 创建任务
    task = await tm.create_task("你好", tenant_id="default")
    
    # Mock LLM 返回 finalize
    mock_response = MagicMock()
    msg = MagicMock()
    msg.content = "你好！有什么我可以帮你的吗？"
    msg.tool_calls = None
    msg.model_dump.return_value = {"role": "assistant", "content": "你好！有什么我可以帮你的吗？"}
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message = msg
    orch.client.chat.completions.create.return_value = mock_response
    
    result = await orch.execute(
        task_id=task.task_id,
        tenant_id="default",
        user_message="你好",
    )
    
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_task_zip_download(full_system):
    """产物 ZIP 下载"""
    tm = full_system["task_manager"]
    
    task = await tm.create_task("测试", tenant_id="default")
    (task.work_dir / "frontend" / "index.html").write_text("<h1>Test</h1>")
    (task.work_dir / "backend" / "main.py").write_text("print('hello')")
    
    zip_path = await tm.create_artifact_zip(task.task_id)
    assert zip_path is not None
    assert Path(zip_path).exists()
    assert Path(zip_path).stat().st_size > 0
