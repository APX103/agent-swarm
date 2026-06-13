"""测试任务管理器"""
import pytest
import asyncio
import tempfile
from pathlib import Path


@pytest.fixture
def task_manager(tmp_path):
    from src.task_manager.manager import TaskManager
    return TaskManager(shared_output_base=str(tmp_path))


@pytest.mark.asyncio
async def test_create_task(task_manager):
    """创建任务"""
    task = await task_manager.create_task("写一个网站", tenant_id="tenant-abc")
    
    assert task.task_id
    assert task.tenant_id == "tenant-abc"
    assert task.user_message == "写一个网站"
    assert task.work_dir.exists()
    assert (task.work_dir / "frontend").is_dir()
    assert (task.work_dir / "backend").is_dir()
    assert (task.work_dir / "_final").is_dir()


@pytest.mark.asyncio
async def test_get_task(task_manager):
    """获取任务"""
    task = await task_manager.create_task("测试任务")
    fetched = task_manager.get_task(task.task_id)
    
    assert fetched is not None
    assert fetched.task_id == task.task_id


@pytest.mark.asyncio
async def test_get_nonexistent_task(task_manager):
    """获取不存在的任务返回 None"""
    assert task_manager.get_task("nonexistent") is None


@pytest.mark.asyncio
async def test_update_status(task_manager):
    """更新任务状态"""
    task = await task_manager.create_task("测试任务")
    
    from src.api.models import TaskStatus
    await task_manager.update_status(task.task_id, TaskStatus.RUNNING)
    
    assert task.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_complete_task(task_manager):
    """完成任务"""
    task = await task_manager.create_task("测试任务")
    
    # 写入一些产物
    (task.work_dir / "frontend" / "index.html").write_text("<h1>Hello</h1>")
    (task.work_dir / "backend" / "main.py").write_text("print('hello')")
    
    await task_manager.complete_task(task.task_id, "全部完成")
    
    assert task.status == "completed"
    assert task.result == "全部完成"
    assert len(task.artifacts) == 2
    assert task.completed_at is not None


@pytest.mark.asyncio
async def test_fail_task(task_manager):
    """任务失败"""
    task = await task_manager.create_task("测试任务")
    
    await task_manager.fail_task(task.task_id, "发生错误")
    
    assert task.status == "failed"
    assert task.result == "发生错误"


@pytest.mark.asyncio
async def test_list_tasks(task_manager):
    """列举任务"""
    t1 = await task_manager.create_task("任务1", tenant_id="t1")
    t2 = await task_manager.create_task("任务2", tenant_id="t1")
    t3 = await task_manager.create_task("任务3", tenant_id="t2")
    
    all_tasks = task_manager.list_tasks()
    assert len(all_tasks) == 3
    
    t1_tasks = task_manager.list_tasks(tenant_id="t1")
    assert len(t1_tasks) == 2
    
    t2_tasks = task_manager.list_tasks(tenant_id="t2")
    assert len(t2_tasks) == 1


@pytest.mark.asyncio
async def test_event_subscription(task_manager):
    """事件订阅"""
    events = []
    
    async def collector(event):
        events.append(event)
    
    task = await task_manager.create_task("测试任务")
    task.subscribe(collector)
    
    from src.api.models import TaskStatus
    await task_manager.update_status(task.task_id, TaskStatus.RUNNING)
    
    assert len(events) == 1
    assert events[0]["type"] == "status_change"
    assert events[0]["new_status"] == "running"


@pytest.mark.asyncio
async def test_create_artifact_zip(task_manager):
    """创建产物压缩包"""
    task = await task_manager.create_task("测试任务")
    (task.work_dir / "frontend" / "index.html").write_text("<h1>Test</h1>")
    
    zip_path = await task_manager.create_artifact_zip(task.task_id)
    assert zip_path is not None
    assert Path(zip_path).exists()
