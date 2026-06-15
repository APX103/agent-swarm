"""任务管理器 - 管理任务生命周期、产物收集、WebSocket 广播"""
import asyncio
import logging
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Awaitable

from src.api.models import TaskStatus

logger = logging.getLogger(__name__)


@dataclass
class Task:
    """任务实例"""
    task_id: str
    tenant_id: str
    user_message: str
    status: TaskStatus = TaskStatus.CREATED
    result: Optional[str] = None
    artifacts: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    work_dir: Optional[Path] = None
    # 事件回调
    _event_subscribers: list[Callable] = field(default_factory=list)
    
    def subscribe(self, callback: Callable[[dict], Awaitable[None]]):
        """订阅任务事件"""
        self._event_subscribers.append(callback)
    
    async def emit_event(self, event: dict):
        """广播事件到所有订阅者"""
        for cb in self._event_subscribers:
            try:
                await cb(event)
            except Exception as e:
                logger.error(f"Error emitting event: {e}")


class TaskManager:
    """任务管理器"""
    
    def __init__(self, shared_output_base: str, store=None):
        self.shared_output_base = Path(shared_output_base)
        self._tasks: dict[str, Task] = {}
        self._lock = asyncio.Lock()
        self._store = store  # optional SQLiteStore for persistence
    
    async def create_task(self, user_message: str, 
                          tenant_id: str = "default") -> Task:
        """创建新任务"""
        task_id = str(uuid.uuid4())[:8]
        
        # 创建工作目录
        work_dir = self.shared_output_base / "tenants" / tenant_id / "tasks" / task_id
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "frontend").mkdir(exist_ok=True)
        (work_dir / "backend").mkdir(exist_ok=True)
        (work_dir / "_final").mkdir(exist_ok=True)
        
        task = Task(
            task_id=task_id,
            tenant_id=tenant_id,
            user_message=user_message,
            work_dir=work_dir,
        )
        
        async with self._lock:
            self._tasks[task_id] = task

        if self._store:
            self._store.save_task(
                task_id=task_id, tenant_id=tenant_id, user_message=user_message,
                status="created", work_dir=str(work_dir),
            )

        logger.info(f"Created task {task_id} for tenant {tenant_id}")
        return task
    
    async def update_status(self, task_id: str, status: TaskStatus):
        """更新任务状态"""
        task = self._tasks.get(task_id)
        if not task:
            return
        
        old_status = task.status
        task.status = status
        
        if status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            task.completed_at = datetime.now()
        
        await task.emit_event({
            "type": "status_change",
            "task_id": task_id,
            "old_status": old_status.value,
            "new_status": status.value,
        })
        
        logger.info(f"Task {task_id}: {old_status.value} -> {status.value}")

        # 持久化状态变更到 SQLite（覆盖 cancelled / running / 任意状态）
        if self._store and task:
            self._store.save_task(
                task_id=task_id, tenant_id=task.tenant_id,
                status=status.value, result=task.result,
                artifacts=task.artifacts,
                work_dir=str(task.work_dir) if task.work_dir else None,
                completed_at=task.completed_at.isoformat() if task.completed_at else None,
            )
    
    async def complete_task(self, task_id: str, result: str):
        """完成任务"""
        task = self._tasks.get(task_id)
        if not task:
            return
        
        task.result = result
        
        # 收集产物列表
        if task.work_dir:
            for role_dir in task.work_dir.iterdir():
                if role_dir.is_dir() and role_dir.name.startswith("_"):
                    continue
                for f in role_dir.rglob("*"):
                    if f.is_file():
                        task.artifacts.append(str(f.relative_to(task.work_dir)))
        
        await self.update_status(task_id, TaskStatus.COMPLETED)

        if self._store and task:
            self._store.save_task(
                task_id=task_id, tenant_id=task.tenant_id,
                status=TaskStatus.COMPLETED.value, result=result,
                artifacts=task.artifacts,
                work_dir=str(task.work_dir) if task.work_dir else None,
                completed_at=datetime.now().isoformat(),
            )

        await task.emit_event({
            "type": "complete",
            "task_id": task_id,
            "result": result,
            "artifacts": task.artifacts,
        })
    
    async def fail_task(self, task_id: str, error: str):
        """标记任务失败"""
        task = self._tasks.get(task_id)
        if task:
            task.result = error
            await self.update_status(task_id, TaskStatus.FAILED)
            await task.emit_event({
                "type": "error",
                "task_id": task_id,
                "message": error,
            })
    
    def _task_from_dict(self, data: dict) -> Task:
        """从 SQLite 行数据重建 Task 对象。"""
        task = Task(
            task_id=data["task_id"],
            tenant_id=data.get("tenant_id", "default"),
            user_message=data.get("user_message", ""),
            status=TaskStatus(data.get("status", "created")),
            result=data.get("result"),
            artifacts=data.get("artifacts", []),
            work_dir=Path(data["work_dir"]) if data.get("work_dir") else None,
        )
        if data.get("created_at"):
            try:
                task.created_at = datetime.fromisoformat(data["created_at"])
            except ValueError:
                pass
        if data.get("completed_at"):
            try:
                task.completed_at = datetime.fromisoformat(data["completed_at"])
            except ValueError:
                pass
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        """获取任务（先查内存缓存，miss 则查 SQLite 恢复）"""
        task = self._tasks.get(task_id)
        if task:
            return task
        if self._store:
            data = self._store.get_task(task_id)
            if data:
                task = self._task_from_dict(data)
                self._tasks[task_id] = task
                return task
        return None
    
    def list_tasks(self, tenant_id: Optional[str] = None) -> list[Task]:
        """列举任务（内存缓存 + SQLite 持久化，重启后也能列出历史任务）。"""
        # 优先使用内存中的 Task 对象（最新状态）
        merged: dict[str, Task] = dict(self._tasks)

        # 从 SQLite 补充历史任务，内存中已存在的不覆盖
        if self._store:
            for data in self._store.list_tasks(tenant_id=tenant_id):
                task_id = data["task_id"]
                if task_id not in merged:
                    merged[task_id] = self._task_from_dict(data)

        tasks = list(merged.values())
        if tenant_id:
            tasks = [t for t in tasks if t.tenant_id == tenant_id]
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)
    
    def get_artifacts_dir(self, task_id: str) -> Optional[Path]:
        """获取任务产物目录"""
        task = self._tasks.get(task_id)
        if task and task.work_dir:
            return task.work_dir
        return None
    
    async def create_artifact_zip(self, task_id: str) -> Optional[str]:
        """创建产物压缩包，返回 zip 路径"""
        task = self._tasks.get(task_id)
        if not task or not task.work_dir:
            return None
        
        zip_path = task.work_dir / f"{task_id}_artifacts.zip"
        if zip_path.exists():
            zip_path.unlink()
        
        await asyncio.to_thread(
            shutil.make_archive,
            str(zip_path.with_suffix("")),
            "zip",
            root_dir=task.work_dir,
            base_dir=".",
        )
        return str(zip_path)
