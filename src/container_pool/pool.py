"""容器池管理器 - 预启动 Docker 容器并管理分配/回收"""
import asyncio
import json
import time
import logging
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path
from enum import Enum

from src.config import BASE_DIR

logger = logging.getLogger(__name__)

# 容器侧硬编码常量（用于 spawn / checkout）
CONTAINER_SHARED_DIR = "/workspace/artifacts"
CONTAINER_CONFIG_PATH = "/etc/swarm/config.json"
CONTAINER_PORT = 9001
READINESS_MAX_WAIT = 30
READINESS_INTERVAL = 2


class ContainerState(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    STARTING = "starting"
    DEAD = "dead"


@dataclass
class PooledContainer:
    """池中容器实例"""
    container_id: str
    container_name: str
    port: int
    state: ContainerState = ContainerState.IDLE
    assigned_task_id: Optional[str] = None
    assigned_role: Optional[str] = None
    use_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    config_file: Optional[str] = None  # 宿主机上挂载的 config.json 路径


class ContainerPoolManager:
    """Docker 容器池管理器
    
    职责：
    1. 启动时预创建 N 个容器（warm pool）
    2. checkout: 从池中获取 idle 容器，注入配置
    3. return: 任务结束后归还容器，清理工作目录
    4. 容器使用计数，超过阈值回收重建
    """
    
    def __init__(self, settings=None):
        self.settings = settings
        self.pool_size = settings.container_pool.pool_size if settings else 5
        self.max_overflow = settings.container_pool.max_overflow if settings else 3
        self.max_uses = settings.container_pool.max_container_uses if settings else 50
        self.image_name = settings.container_pool.image_name if settings else "swarm-worker:latest"
        self.network_name = settings.container_pool.network_name if settings else "swarm-net"
        self.base_port = settings.container_pool.base_port if settings else 9001
        self.mem_limit = settings.container_pool.mem_limit if settings else "512m"
        self.cpu_limit = settings.container_pool.cpu_limit if settings else 0.5
        self.worker_host = settings.container_pool.worker_host if settings else "localhost"
        self.worker_dev_mode = settings.container_pool.worker_dev_mode if settings else False

        self.shared_output_base = settings.storage.shared_output_base if settings else str(BASE_DIR / "shared_output")
        
        self._pool: dict[str, PooledContainer] = {}  # container_id -> PooledContainer
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._lock = asyncio.Lock()
        self._client = None  # Docker client, lazy init
        self._config_dir: Optional[Path] = None
        
    @property
    def client(self):
        """懒加载 Docker client"""
        if self._client is None:
            import docker
            self._client = docker.from_env()
        return self._client
    
    @property
    def semaphore(self):
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.pool_size + self.max_overflow)
        return self._semaphore
    
    async def startup(self) -> None:
        """启动容器池：创建网络、拉取镜像、预启动容器"""
        logger.info(f"Starting container pool: size={self.pool_size}, image={self.image_name}")

        # 准备配置目录：容器化时必须是 host 可见路径（pool 把它 bind-mount 进 worker，
        # 而 worker 由 host daemon 创建）；否则用项目根目录下的 .pool_configs。
        configured = getattr(self.settings.container_pool, "pool_config_dir", "") if self.settings else ""
        if configured:
            self._config_dir = Path(configured)
        else:
            project_root = Path(__file__).parent.parent.parent
            self._config_dir = project_root / ".pool_configs"
        self._config_dir.mkdir(parents=True, exist_ok=True)
        
        # 创建 Docker 网络
        try:
            self.client.networks.create(self.network_name, check_duplicate=True)
            logger.info(f"Created network: {self.network_name}")
        except Exception as e:
            logger.debug(f"Network already exists or error: {e}")
        
        # 确保镜像存在
        try:
            self.client.images.get(self.image_name)
            logger.info(f"Image found: {self.image_name}")
        except Exception:
            logger.warning(f"Image not found: {self.image_name}. Building...")
            await self._build_image()
        
        # 预启动容器：先清理可能残留的同名容器，避免 409 Conflict
        for i in range(self.pool_size):
            container_name = f"swarm-worker-{i}"
            try:
                try:
                    old = self.client.containers.get(container_name)
                    old.stop(timeout=3)
                    old.remove(force=True)
                    logger.info(f"Removed stale container {container_name}")
                except Exception:
                    logger.warning("Could not remove stale container %s", container_name, exc_info=True)
                container = await self._spawn_container(i)
                if container:
                    self._pool[container.container_id] = container
                    logger.info(f"Pre-started container {i}: {container.container_name} (port {container.port})")
            except Exception as e:
                logger.error(f"Failed to start container {i}: {e}", exc_info=True)
        
        logger.info(f"Container pool ready: {len(self._pool)} containers")
    
    async def _build_image(self) -> None:
        """构建 Worker 镜像"""
        # Dockerfile 在 docker/ 目录，但 build context 是项目根目录
        dockerfile_path = Path(__file__).parent.parent.parent / "docker" / "Dockerfile.worker"
        project_root = dockerfile_path.parent.parent  # 项目根目录
        if dockerfile_path.exists():
            self.client.images.build(
                path=str(project_root),
                dockerfile=str(dockerfile_path.relative_to(project_root)),
                tag=self.image_name,
                rm=True,
            )
        else:
            logger.warning(f"No Dockerfile found at {dockerfile_path}, skipping build")
    
    async def _spawn_container(self, index: int) -> Optional[PooledContainer]:
        """启动一个 warm 容器"""
        host_port = self.base_port + index  # 宿主机端口: 9001, 9002, 9003...
        container_name = f"swarm-worker-{index}"
        config_file = str(self._config_dir / f"container-{index}-config.json")
        
        # 确保配置文件存在（空文件），Docker bind mount 需要源文件存在
        Path(config_file).parent.mkdir(parents=True, exist_ok=True)
        Path(config_file).touch()
        
        try:
            container = self.client.containers.run(
                self.image_name,
                name=container_name,
                detach=True,
                network=self.network_name,
                ports={f"{CONTAINER_PORT}/tcp": host_port},  # 容器内固定端口，宿主机递增
                volumes={
                    self.shared_output_base: {"bind": CONTAINER_SHARED_DIR, "mode": "rw"},
                    config_file: {"bind": CONTAINER_CONFIG_PATH, "mode": "ro"},
                } if not self.worker_dev_mode else {
                    self.shared_output_base: {"bind": CONTAINER_SHARED_DIR, "mode": "rw"},
                    config_file: {"bind": CONTAINER_CONFIG_PATH, "mode": "ro"},
                    str(Path(__file__).parent.parent.parent / "src" / "agents"): {"bind": "/app/agents", "mode": "rw"},
                },
                mem_limit=self.mem_limit,
                nano_cpus=int(self.cpu_limit * 1e9),
                environment={
                    "CONTAINER_INDEX": str(index),
                    "CONTAINER_PORT": str(CONTAINER_PORT),  # 容器内始终使用固定端口
                    "WAIT_FOR_CONFIG": "true",  # 等待配置注入后启动
                },
            )
            
            return PooledContainer(
                container_id=container.id,
                container_name=container_name,
                port=host_port,  # 返回宿主机端口，供 orchestrator 通过 localhost 访问
                config_file=config_file,
            )
        except Exception as e:
            logger.error(f"Failed to spawn container {index}: {e}", exc_info=True)
            return None
    
    async def checkout(self, agent_card_id: str, task_id: str,
                       model: str, base_url: str, api_key: str,
                       tenant_id: Optional[str] = None,
                       orchestrator_url: Optional[str] = None,
                       shared_dir_override: Optional[str] = None) -> Optional[PooledContainer]:
        """从池中获取一个容器，注入配置
        
        Args:
            agent_card_id: Agent 类型 ID（如 "frontend-ux-pro"）
            task_id: 任务 ID
            model: LLM 模型名
            base_url: LLM API base URL
            api_key: LLM API key
            tenant_id: 租户 ID
            orchestrator_url: Orchestrator 的 A2A URL
        
        Returns:
            分配的容器，或 None（池耗尽）
        """
        async with self._lock:
            # 找到 idle 且存活 的容器
            idle = None
            for cid, c in self._pool.items():
                if c.state == ContainerState.IDLE:
                    # 检查容器是否真的在运行（防止 checkout 到已退出的僵尸容器）
                    # 只在真实 docker client 下检查（mock 环境跳过）
                    if self._client is not None and not isinstance(self._client, type(None)):
                        try:
                            info = self._client.containers.get(c.container_id)
                            if info.status != "running":
                                logger.warning("Container %s is %s (not running) — marking DEAD", c.container_name, info.status)
                                c.state = ContainerState.DEAD
                                continue
                        except Exception:
                            logger.warning("Container %s not found — marking DEAD", c.container_name)
                            c.state = ContainerState.DEAD
                            continue
                    idle = c
                    break
            
            if idle is None:
                logger.warning("No idle+running containers available")
                return None
            
            # 标记为 busy
            idle.state = ContainerState.BUSY
            idle.assigned_task_id = task_id
            idle.assigned_role = agent_card_id
            idle.use_count += 1
            idle.last_used_at = time.time()
        
        # 注入配置
        # shared_dir 指向容器的 task 工作目录（或 session 工作目录，如果 override 给定）
        if shared_dir_override:
            try:
                rel = Path(shared_dir_override).relative_to(self.shared_output_base)
                container_shared_dir = f"{CONTAINER_SHARED_DIR}/{rel}"
            except (ValueError, TypeError):
                container_shared_dir = shared_dir_override
        else:
            container_shared_dir = f"{CONTAINER_SHARED_DIR}/tenants/{tenant_id or 'default'}/tasks/{task_id}"
        config = {
            "task_id": task_id,
            "tenant_id": tenant_id or "default",
            "agent_role": agent_card_id,
            "model": model,
            "base_url": base_url,
            "api_key": api_key,
            "port": CONTAINER_PORT,  # 容器内部始终使用固定端口（Docker 映射到宿主机 idle.port）
            "shared_dir": container_shared_dir,
            "orchestrator_url": orchestrator_url,
        }

        # 注入角色 system_prompt（来自 settings.agent_cards）：支持纯 config 定义的新角色
        if self.settings:
            for _card in getattr(self.settings, "agent_cards", []):
                if getattr(_card, "id", None) == agent_card_id:
                    _sp = getattr(_card, "system_prompt", "")
                    if _sp:
                        config["system_prompt"] = _sp
                    break
        
        config_path = Path(idle.config_file)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, indent=2))
        
        logger.info(f"Checked out container {idle.container_name} for task {task_id} (role: {agent_card_id})")
        
        # 等待 Worker 就绪（轮询健康检查）
        import httpx
        max_wait = READINESS_MAX_WAIT
        for _ in range(max_wait // READINESS_INTERVAL):
            try:
                async with httpx.AsyncClient(timeout=3.0) as hc:
                    resp = await hc.get(f"http://{self.worker_host}:{idle.port}/.well-known/agent.json")
                    if resp.status_code == 200:
                        logger.info(f"Worker {idle.container_name} ready on port {idle.port}")
                        break
            except Exception:
                logger.warning("Worker readiness check failed for %s (port %s), retrying...",
                             idle.container_name, idle.port, exc_info=True)
            await asyncio.sleep(READINESS_INTERVAL)
        else:
            # Worker didn't become ready in time. In degraded/mock mode there's no
            # HTTP server so this always times out — we still return the container
            # (the LLM call may work via direct A2A). Real readiness failure is
            # caught downstream when the dispatch fails and the breaker trips.
            logger.warning(f"Worker {idle.container_name} did not become ready after {max_wait}s (continuing anyway)")

        return idle
    
    async def return_container(self, container_id: str) -> None:
        """归还容器到池中"""
        async with self._lock:
            container = self._pool.get(container_id)
            if not container:
                logger.warning(f"Container {container_id} not in pool")
                return

            container.state = ContainerState.IDLE
            container.assigned_task_id = None
            container.assigned_role = None

            # 不清理容器内文件——/workspace/artifacts 是 shared_output 的 bind mount，
            # rm -rf 会删掉宿主机的 swarm.db 和其他重要文件。
            # 跨任务串扰通过 per-task SHARED_DIR（contextvar）避免，不需要清理容器。

            # 注意：不重置 config.json 为 {}。reset 会导致 worker 的 reload_config
            # 读到空配置，丢失 model/api_key/shared_dir，下一次 checkout 时如果
            # reload 时机不对会用默认值（如 glm-coding-plan 而非 glm-4.7）。
            # checkout 时会写入新 config 覆盖旧值，所以这里保留即可。
            # （原代码：Path(container.config_file).write_text("{}")）

            # 检查是否需要回收
            if container.use_count >= self.max_uses:
                logger.info(f"Recycling container {container.container_name} (uses={container.use_count})")
                await self._recycle_container(container)

        logger.info(f"Returned container {container.container_name} to pool")

    # _clean_container_artifacts 已移除：
    # 它执行 docker exec rm -rf /workspace/artifacts/*，但 /workspace/artifacts 是
    # shared_output 的 bind mount，会删掉宿主机的 swarm.db。跨任务隔离通过
    # per-task SHARED_DIR (contextvar) 实现，不需要清理容器文件系统。
    
    async def _recycle_container(self, container: PooledContainer):
        """回收并重建容器"""
        try:
            old = self.client.containers.get(container.container_id)
            old.stop(timeout=5)
            old.remove()
        except Exception as e:
            logger.debug(f"Error removing old container: {e}")
        
        # 从名字提取 index
        index = int(container.container_name.split("-")[-1])
        new_container = await self._spawn_container(index)
        if new_container:
            del self._pool[container.container_id]
            self._pool[new_container.container_id] = new_container
    
    async def shutdown(self) -> None:
        """关闭容器池"""
        logger.info("Shutting down container pool...")
        for container in self._pool.values():
            try:
                c = self.client.containers.get(container.container_id)
                c.stop(timeout=5)
                c.remove()
            except Exception:
                logger.warning("Error shutting down container %s", container.container_name, exc_info=True)
        self._pool.clear()
        if self._client:
            self._client.close()
        logger.info("Container pool shut down")
    
    def get_status(self) -> dict:
        """获取池状态"""
        idle = sum(1 for c in self._pool.values() if c.state == ContainerState.IDLE)
        busy = sum(1 for c in self._pool.values() if c.state == ContainerState.BUSY)
        return {
            "total": len(self._pool),
            "idle": idle,
            "busy": busy,
            "overflow_capable": self.max_overflow,
        }
