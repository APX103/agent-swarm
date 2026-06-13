"""Agent Swarm - 全局配置"""
import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


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


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000


@dataclass
class Settings:
    server: ServerConfig = field(default_factory=ServerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    container_pool: ContainerPoolConfig = field(default_factory=ContainerPoolConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    agent_cards: list[AgentCardDef] = field(default_factory=list)


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

    if "agent_cards" in data:
        for card in data["agent_cards"]:
            settings.agent_cards.append(AgentCardDef(
                id=card["id"],
                name=card["name"],
                description=card["description"],
                skills=card.get("skills", []),
            ))

    return settings


# 全局单例
settings = load_settings()
