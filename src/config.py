"""Agent Swarm - 全局配置"""
import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from src.orchestrator.base import OrchestratorConfig


BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = os.environ.get("SWARM_CONFIG", str(BASE_DIR / "config" / "default.yaml"))


@dataclass
class LLMConfig:
    default_model: str = "glm-coding-plan"
    default_base_url: str = "https://open.bigmodel.cn/api/coding/paas/v4"
    default_api_key: str = ""

    def __post_init__(self):
        if not self.default_api_key:
            self.default_api_key = os.environ.get("GLM_API_KEY", "")


@dataclass
class ContainerPoolConfig:
    pool_size: int = 5
    max_overflow: int = 3
    max_container_uses: int = 50
    image_name: str = "swarm-worker:latest"
    network_name: str = "swarm-net"
    base_port: int = 9001
    mem_limit: str = "512m"
    cpu_limit: float = 0.5
    worker_host: str = "localhost"  # host where worker ports are published (host.docker.internal when orchestrator runs in a container)
    pool_config_dir: str = ""  # host-visible dir for per-container config.json (container mode must set this to a host path)


@dataclass
class StorageConfig:
    shared_output_base: str = "/home/apx103/work/swarm/shared_output"


@dataclass
class TimeoutConfig:
    agent_startup: int = 30
    task_execution: int = 600
    container_checkout: int = 10


@dataclass
class AgentCardDef:
    id: str
    name: str
    description: str
    skills: list = field(default_factory=list)
    system_prompt: str = ""  # optional; lets new roles be defined purely in config


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000


@dataclass
class RedisConfig:
    redis_url: str = "redis://localhost:6379"
    heartbeat_ttl: int = 30
    heartbeat_interval: int = 10


@dataclass
class Settings:
    server: ServerConfig = field(default_factory=ServerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    container_pool: ContainerPoolConfig = field(default_factory=ContainerPoolConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    agent_cards: list[AgentCardDef] = field(default_factory=list)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    api_key: str = ""  # if set, /api/v1/ routes require X-API-Key header
    dispatcher: dict = field(default_factory=lambda: {
        "max_retries": 2, "dispatch_timeout": 300.0, "max_concurrent": 8, "health_precheck": True,
    })


def load_settings(config_path: Optional[str] = None) -> Settings:
    """从 YAML 文件加载配置"""
    path = Path(config_path or CONFIG_PATH)
    if not path.exists():
        return Settings()

    with open(path, "r") as f:
        data = yaml.safe_load(f) or {}

    settings = Settings()

    if "server" in data:
        settings.server = ServerConfig(**data["server"])

    if "llm" in data:
        llm_data = data["llm"]
        settings.llm = LLMConfig(
            default_model=llm_data.get("default_model", settings.llm.default_model),
            default_base_url=llm_data.get("default_base_url", settings.llm.default_base_url),
            default_api_key=llm_data.get("default_api_key", ""),
        )

    if "container_pool" in data:
        settings.container_pool = ContainerPoolConfig(**data["container_pool"])

    if "storage" in data:
        settings.storage = StorageConfig(**data["storage"])

    if "timeouts" in data:
        settings.timeouts = TimeoutConfig(**data["timeouts"])

    if "redis" in data:
        settings.redis = RedisConfig(**data["redis"])

    if "agent_cards" in data:
        for card in data["agent_cards"]:
            settings.agent_cards.append(AgentCardDef(
                id=card["id"],
                name=card["name"],
                description=card["description"],
                skills=card.get("skills", []),
                system_prompt=card.get("system_prompt", ""),
            ))

    if "orchestrator" in data:
        o = data["orchestrator"]
        settings.orchestrator = OrchestratorConfig(
            provider=o.get("provider", "builtin"),
            external_endpoint=o.get("external_endpoint", ""),
            external_timeout=float(o.get("external_timeout", 600.0)),
            fallback=bool(o.get("fallback", True)),
        )

    if "dispatcher" in data:
        d = data["dispatcher"]
        settings.dispatcher = {
            "max_retries": int(d.get("max_retries", 2)),
            "dispatch_timeout": float(d.get("dispatch_timeout", 300.0)),
            "max_concurrent": int(d.get("max_concurrent", 8)),
            "health_precheck": bool(d.get("health_precheck", True)),
        }

    # API key (optional, for /api/v1/ protection)
    if os.environ.get("SWARM_API_KEY"):
        settings.api_key = os.environ["SWARM_API_KEY"]

    # Environment overrides for orchestrator selection.
    if os.environ.get("ORCHESTRATOR_PROVIDER"):
        settings.orchestrator.provider = os.environ["ORCHESTRATOR_PROVIDER"]
    if os.environ.get("ORCHESTRATOR_EXTERNAL_ENDPOINT"):
        settings.orchestrator.external_endpoint = os.environ["ORCHESTRATOR_EXTERNAL_ENDPOINT"]

    # Environment overrides (non-empty only) for deployment knobs (compose/container).
    if os.environ.get("LLM_DEFAULT_MODEL"):
        settings.llm.default_model = os.environ["LLM_DEFAULT_MODEL"]
    if os.environ.get("LLM_DEFAULT_BASE_URL"):
        settings.llm.default_base_url = os.environ["LLM_DEFAULT_BASE_URL"]
    if os.environ.get("LLM_DEFAULT_API_KEY"):
        settings.llm.default_api_key = os.environ["LLM_DEFAULT_API_KEY"]
    if os.environ.get("SHARED_OUTPUT_BASE"):
        settings.storage.shared_output_base = os.environ["SHARED_OUTPUT_BASE"]
    if os.environ.get("CONTAINER_POOL_SIZE"):
        try:
            settings.container_pool.pool_size = int(os.environ["CONTAINER_POOL_SIZE"])
        except ValueError:
            pass
    if os.environ.get("CONTAINER_BASE_PORT"):
        try:
            settings.container_pool.base_port = int(os.environ["CONTAINER_BASE_PORT"])
        except ValueError:
            pass
    if os.environ.get("CONTAINER_IMAGE_NAME"):
        settings.container_pool.image_name = os.environ["CONTAINER_IMAGE_NAME"]
    if os.environ.get("CONTAINER_WORKER_HOST"):
        settings.container_pool.worker_host = os.environ["CONTAINER_WORKER_HOST"]
    if os.environ.get("POOL_CONFIG_DIR"):
        settings.container_pool.pool_config_dir = os.environ["POOL_CONFIG_DIR"]
    if os.environ.get("REDIS_URL"):
        settings.redis.redis_url = os.environ["REDIS_URL"]

    return settings


# 全局单例
settings = load_settings()


def validate_settings(s: "Settings") -> list[str]:
    """Return a list of human-readable warnings about likely-misconfigured settings.

    Called at startup so operators see problems early (fail-fast / defensive).
    An empty list means no issues detected.
    """
    warnings: list[str] = []

    if not s.llm.default_api_key:
        warnings.append("llm.default_api_key is empty; worker dispatch will fail without a key")
    if not s.llm.default_base_url:
        warnings.append("llm.default_base_url is empty")

    if s.orchestrator.provider == "external" and not s.orchestrator.external_endpoint:
        warnings.append(
            "orchestrator.provider='external' but external_endpoint is empty; "
            "the resolver will fall back to builtin"
        )

    if s.container_pool.pool_size <= 0:
        warnings.append("container_pool.pool_size <= 0; no warm workers will be available")

    if not s.storage.shared_output_base:
        warnings.append("storage.shared_output_base is empty; artifacts cannot be stored")

    return warnings
