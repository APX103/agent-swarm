"""Agent Swarm - FastAPI 入口"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

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
from src.session.service import SessionService
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
    import sqlite3 as _sqlite3
    _db_path = settings.storage.shared_output_base + "/swarm.db"
    store = SQLiteStore(_db_path)
    session_svc = SessionService(_db_path, settings.storage.shared_output_base)
    # 验证表确实建好了
    _v = _sqlite3.connect(_db_path)
    _t = {r[0] for r in _v.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    _v.close()
    if not {"tasks","sessions","sessions_v2"}.issubset(_t):
        logger.error("DB tables missing! Forcing re-creation: %s", _t)
        _f = _sqlite3.connect(_db_path)
        _f.executescript("CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, tenant_id TEXT DEFAULT 'default', session_id TEXT, user_message TEXT, status TEXT DEFAULT 'created', result TEXT, artifacts TEXT DEFAULT '[]', work_dir TEXT, created_at TEXT, completed_at TEXT); CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, tenant_id TEXT DEFAULT 'default', work_dir TEXT NOT NULL, messages TEXT DEFAULT '[]', shared_context TEXT DEFAULT '', created_at REAL); CREATE TABLE IF NOT EXISTS sessions_v2 (session_id TEXT PRIMARY KEY, tenant_id TEXT DEFAULT 'default', work_dir TEXT NOT NULL, state TEXT DEFAULT '{}', events TEXT DEFAULT '[]', created_at REAL);")
        _f.commit(); _f.close()
    logger.info("DB initialized: %s", _db_path)
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
    gateway.set_deps(
        registry, adapter_manager,
        task_manager=None,  # filled after task_manager is created (see below)
    )
    logger.info("AdapterManager initialized")

    # 3b. 声明式 Agent 注册 (agents/*.yaml)
    _agents_dir = Path("agents")
    if _agents_dir.exists():
        import yaml as _yaml
        from src.adapters.adapter_manager import create_adapter as _create_adapter
        for _f in sorted(_agents_dir.glob("*.yaml")):
            try:
                _data = _yaml.safe_load(_f.read_text())
                _proto = _data.get("protocol", "http")
                _agent_id = await registry.register({
                    "name": _data["name"], "endpoint": _data["endpoint"],
                    "protocol": _proto, "skills": _data.get("skills", []),
                }, internal=True)
                if _proto in ("openai", "cli", "mcp", "a2a"):
                    _info = {"protocol": _proto}
                    _info["base_url" if _proto in ("openai", "a2a") else "server_url" if _proto == "mcp" else "command"] = _data["endpoint"]
                    _info.update({k: v for k, v in _data.items() if k not in ("name", "endpoint", "protocol", "skills")})
                    adapter_manager.register(_agent_id, _create_adapter(_info))
                logger.info("Registered declared agent: %s (%s)", _data.get("name"), _f.name)
            except Exception as _e:
                logger.warning("Failed to register agent from %s: %s", _f.name, _e)
    
    # 4. 初始化容器池
    pool_manager = ContainerPoolManager(settings=settings)
    try:
        await pool_manager.startup()
        logger.info("ContainerPool initialized")
    except Exception as e:
        logger.error(f"ContainerPool startup failed (running in mock mode): {e}")
        logger.warning("Will operate without real Docker containers")
    
    # 5. 初始化统一 Dispatcher（Docker 容器 + 外部注册 Agent 同为候选）
    dcfg = settings.dispatcher
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
        DispatcherConfig(
            max_retries=dcfg["max_retries"],
            dispatch_timeout=dcfg["dispatch_timeout"],
            max_concurrent=dcfg["max_concurrent"],
            health_precheck=dcfg["health_precheck"],
        ),
        result_cache=ResultCache(),
    )

    # 6. 初始化编排器（注入 Dispatcher）
    orchestrator = Orchestrator(
        settings=settings,
        pool_manager=pool_manager,
        task_manager=task_manager,
        dispatcher=dispatcher,
        session_service=session_svc,
    )
    logger.info("Orchestrator initialized (unified dispatcher wired)")

    # 7. 组装 session 管理器 + 可插拔编排器解析器并注入依赖
    session_mgr = SessionManager(settings.storage.shared_output_base, store=store)
    resolver = OrchestratorResolver(builtin=orchestrator, config=settings.orchestrator)
    resolver.set_session_service(session_svc)
    set_deps(orchestrator, task_manager, pool_manager, resolver=resolver, sess_mgr=session_mgr, session_svc=session_svc, dispatcher=dispatcher)

    # 8. 把直聊增强所需的依赖回填到 gateway（task_manager/session/dispatcher 在 3 之后才就绪）
    gateway.set_deps(
        registry, adapter_manager,
        task_manager=task_manager, session_manager=session_mgr, session_service=session_svc, dispatcher=dispatcher,
    )
    logger.info("Gateway direct-chat deps wired")
    
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

    # 交互式聊天 UI 已下线：交互统一收敛到 eino-agent，Swarm 只保留监控 Dashboard。
    # /ui 与 /ui/ 重定向到 /dashboard/。
    from fastapi.responses import RedirectResponse

    @app.get("/ui")
    async def _redirect_ui():
        return RedirectResponse(url="/dashboard/")

    @app.get("/ui/")
    async def _redirect_ui_slash():
        return RedirectResponse(url="/dashboard/")

    # 挂载监控仪表板（dashboard/ 目录，通过配置开关控制）。
    if settings.dashboard.enabled:
        _dashboard_dir = Path(__file__).parent.parent / "dashboard"
        if _dashboard_dir.exists():
            from fastapi.staticfiles import StaticFiles
            app.mount("/dashboard", StaticFiles(directory=str(_dashboard_dir), html=True), name="dashboard")

    # API Key 保护 /api/v1/ 路由（内网防误调用）
    if settings.api_key:
        @app.middleware("http")
        async def api_key_guard(request, call_next):
            if request.url.path.startswith("/api/v1/"):
                if request.headers.get("X-API-Key") != settings.api_key:
                    from fastapi.responses import JSONResponse
                    return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})
            return await call_next(request)

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
