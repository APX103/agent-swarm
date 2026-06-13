"""Agent Swarm - FastAPI 入口"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import settings
from src.task_manager.manager import TaskManager
from src.container_pool.pool import ContainerPoolManager
from src.orchestrator.orchestrator import Orchestrator
from src.api.routes import router, set_deps

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 全局依赖
pool_manager: ContainerPoolManager = None
task_manager: TaskManager = None
orchestrator: Orchestrator = None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """应用生命周期管理"""
    global pool_manager, task_manager, orchestrator
    
    logger.info("🐝 Agent Swarm starting up...")
    
    # 1. 初始化任务管理器
    task_manager = TaskManager(
        shared_output_base=settings.storage.shared_output_base,
    )
    logger.info("TaskManager initialized")
    
    # 2. 初始化容器池
    pool_manager = ContainerPoolManager(settings=settings)
    try:
        await pool_manager.startup()
        logger.info("ContainerPool initialized")
    except Exception as e:
        logger.error(f"ContainerPool startup failed (running in mock mode): {e}")
        logger.warning("Will operate without real Docker containers")
    
    # 3. 初始化编排器
    orchestrator = Orchestrator(
        settings=settings,
        pool_manager=pool_manager,
        task_manager=task_manager,
    )
    logger.info("Orchestrator initialized")
    
    # 4. 注入依赖
    set_deps(orchestrator, task_manager, pool_manager)
    
    logger.info("🐝 Agent Swarm ready!")
    
    yield
    
    # 清理
    logger.info("🐝 Agent Swarm shutting down...")
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
