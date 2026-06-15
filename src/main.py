"""Agent Swarm - FastAPI 入口"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import settings, validate_settings
from src.task_manager.manager import TaskManager
from src.container_pool.pool import ContainerPoolManager
from src.orchestrator.orchestrator import Orchestrator
from src.api.routes import router, set_deps
from src.registry.registry import AgentRegistry
from src.adapters.adapter_manager import AdapterManager
from src.gateway import routes as gateway
from src.dispatcher.backends import DockerBackend, ExternalAgentBackend
from src.dispatcher.dispatcher import Dispatcher, DispatcherConfig
from src.dispatcher.result_cache import ResultCache
from src.orchestrator.resolver import OrchestratorResolver
from src.session.manager import SessionManager
from src.observability.trace import TraceIdFilter
from src.registry.sweeper import health_sweep_loop

_swarm_handler = logging.StreamHandler()
_swarm_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] [trace=%(trace_id)s] %(name)s: %(message)s")
)
_swarm_handler.addFilter(TraceIdFilter())
logging.basicConfig(level=logging.INFO, handlers=[_swarm_handler])
logger = logging.getLogger(__name__)

# 全局依赖
pool_manager: ContainerPoolManager = None
task_manager: TaskManager = None
orchestrator: Orchestrator = None
registry: AgentRegistry = None
adapter_manager: AdapterManager = None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """应用生命周期管理"""
    global pool_manager, task_manager, orchestrator, registry, adapter_manager
    
    logger.info("🐝 Agent Swarm starting up...")

    # 0. 启动期配置自检（fail-fast 警告，不阻断启动）
    for warning in validate_settings(settings):
        logger.warning("config check: %s", warning)

    # 1. 初始化持久化 + 任务管理器
    from src.storage.sqlite_store import SQLiteStore
    store = SQLiteStore(settings.storage.shared_output_base + "/swarm.db")
    task_manager = TaskManager(
        shared_output_base=settings.storage.shared_output_base,
        store=store,
    )
    logger.info("TaskManager initialized")
    
    # 2. 初始化 Agent 注册中心
    registry = AgentRegistry(
        redis_url=settings.redis.redis_url,
        heartbeat_ttl=settings.redis.heartbeat_ttl,
    )
    _sweep_task = None
    try:
        await registry.connect()
        logger.info("AgentRegistry connected to Redis")
        _sweep_task = asyncio.create_task(health_sweep_loop(registry, interval=60.0))
    except Exception as e:
        logger.warning(f"AgentRegistry Redis connect failed (running degraded): {e}")
    
    # 3. 初始化适配器管理器
    adapter_manager = AdapterManager()
    gateway.set_deps(registry, adapter_manager)
    logger.info("AdapterManager initialized")
    
    # 4. 初始化容器池
    pool_manager = ContainerPoolManager(settings=settings)
    try:
        await pool_manager.startup()
        logger.info("ContainerPool initialized")
    except Exception as e:
        logger.error(f"ContainerPool startup failed (running in mock mode): {e}")
        logger.warning("Will operate without real Docker containers")
    
    # 5. 初始化统一 Dispatcher（Docker 容器 + 外部注册 Agent 同为候选）
    dispatcher = Dispatcher(
        [
            DockerBackend(
                pool=pool_manager,
                model=settings.llm.default_model,
                base_url=settings.llm.default_base_url,
                api_key=settings.llm.default_api_key or "no-key-configured",
                worker_host=settings.container_pool.worker_host,
            ),
            ExternalAgentBackend(registry=registry, adapter_manager=adapter_manager),
        ],
        DispatcherConfig(),
        result_cache=ResultCache(),
    )

    # 6. 初始化编排器（注入 Dispatcher）
    orchestrator = Orchestrator(
        settings=settings,
        pool_manager=pool_manager,
        task_manager=task_manager,
        dispatcher=dispatcher,
    )
    logger.info("Orchestrator initialized (unified dispatcher wired)")

    # 7. 组装 session 管理器 + 可插拔编排器解析器并注入依赖
    session_mgr = SessionManager(settings.storage.shared_output_base, store=store)
    resolver = OrchestratorResolver(builtin=orchestrator, config=settings.orchestrator)
    set_deps(orchestrator, task_manager, pool_manager, resolver=resolver, sess_mgr=session_mgr)
    
    logger.info("🐝 Agent Swarm ready!")
    
    yield
    
    # 清理
    logger.info("🐝 Agent Swarm shutting down...")
    if _sweep_task:
        _sweep_task.cancel()
        try:
            await _sweep_task
        except asyncio.CancelledError:
            pass
    if registry:
        try:
            await registry.close()
        except Exception:
            pass
    if pool_manager:
        await pool_manager.shutdown()
    logger.info("👋 Goodbye!")


def create_app(lifespan=None) -> FastAPI:
    """创建 FastAPI 应用"""
    app = FastAPI(
        title="Agent Swarm",
        description="Docker-based Multi-Agent Swarm with A2A Protocol",
        version="1.0.0",
        lifespan=lifespan or _lifespan,
    )
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    app.include_router(router)
    app.include_router(gateway.router)
    
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=True,
    )
