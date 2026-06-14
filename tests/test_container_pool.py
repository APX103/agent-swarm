"""测试容器池管理器"""
import pytest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class ContainerState(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    ERROR = "error"


@dataclass
class ContainerInfo:
    """容器信息"""
    container_id: str
    container_name: str
    port: int
    state: ContainerState = ContainerState.IDLE
    assigned_task_id: Optional[str] = None
    assigned_role: Optional[str] = None
    use_count: int = 0


@dataclass
class MockSettings:
    class ContainerPool:
        pool_size = 2
        max_overflow = 1
        max_container_uses = 50
        image_name = "test-image:latest"
        network_name = "test-net"
        base_port = 9100
        mem_limit = "256m"
        cpu_limit = 0.5
        worker_host = "localhost"

    class Storage:
        shared_output_base = "/tmp/swarm-test/shared_output"

    class LLM:
        default_model = "test-model"
        default_base_url = "https://test.example.com/v4"
        default_api_key = "test-key"

    container_pool = ContainerPool()
    storage = Storage()
    llm = LLM()
    agent_cards = []


@pytest.fixture
def settings():
    return MockSettings()


@pytest.fixture
def mock_docker_client():
    """Mock Docker client"""
    client = MagicMock()
    call_count = {"n": 0}

    def make_mock_container(*args, **kwargs):
        call_count["n"] += 1
        c = MagicMock()
        c.id = f"container-abc{call_count['n']}"
        c.stop = MagicMock()
        c.remove = MagicMock()
        return c

    client.containers.run = MagicMock(side_effect=make_mock_container)
    client.containers.get = MagicMock(side_effect=make_mock_container)

    # Mock images
    client.images.get = MagicMock(return_value=MagicMock())

    # Mock networks
    client.networks.create = MagicMock()

    return client


@pytest.mark.asyncio
async def test_pool_initialization(settings, mock_docker_client):
    """测试容器池初始化"""
    from src.container_pool.pool import ContainerPoolManager

    pool = ContainerPoolManager(settings=settings)
    pool._client = mock_docker_client

    await pool.startup()

    status = pool.get_status()
    assert status["total"] == 2
    assert status["idle"] == 2

    await pool.shutdown()


@pytest.mark.asyncio
async def test_checkout_return_cycle(settings, mock_docker_client):
    """测试 checkout -> return 生命周期"""
    from src.container_pool.pool import ContainerPoolManager

    pool = ContainerPoolManager(settings=settings)
    pool._client = mock_docker_client

    await pool.startup()

    # Checkout 一个容器
    container = await pool.checkout(
        agent_card_id="frontend-ux-pro",
        task_id="task-001",
        model="test-model",
        base_url="https://test.example.com/v4",
        api_key="test-key",
    )

    assert container is not None
    assert container.state == ContainerState.BUSY
    assert container.assigned_task_id == "task-001"
    assert container.assigned_role == "frontend-ux-pro"

    # Pool 状态更新
    status = pool.get_status()
    assert status["busy"] == 1
    assert status["idle"] == 1

    # Return 容器
    await pool.return_container(container.container_id)

    status = pool.get_status()
    assert status["busy"] == 0
    assert status["idle"] == 2

    await pool.shutdown()


@pytest.mark.asyncio
async def test_checkout_when_pool_empty(settings, mock_docker_client):
    """池空时 checkout 返回 None"""
    from src.container_pool.pool import ContainerPoolManager

    pool = ContainerPoolManager(settings=settings)
    pool._client = mock_docker_client
    pool._pool = {}  # 空池

    container = await pool.checkout(
        agent_card_id="test",
        task_id="task-001",
        model="test",
        base_url="https://test",
        api_key="key",
    )

    assert container is None


@pytest.mark.asyncio
async def test_get_status(settings, mock_docker_client):
    """测试状态查询"""
    from src.container_pool.pool import ContainerPoolManager

    pool = ContainerPoolManager(settings=settings)
    pool._client = mock_docker_client

    status = pool.get_status()
    assert "total" in status
    assert "idle" in status
    assert "busy" in status
    assert status["total"] == 0


@pytest.mark.asyncio
async def test_shutdown_empty_pool(settings, mock_docker_client):
    """空池关闭不报错"""
    from src.container_pool.pool import ContainerPoolManager

    pool = ContainerPoolManager(settings=settings)
    pool._client = mock_docker_client
    pool._pool = {}

    await pool.shutdown()  # 不应抛异常
