"""测试配置加载"""
import pytest
import os
import tempfile
from pathlib import Path


def test_load_default_config():
    """加载默认配置"""
    from src.config import load_settings
    settings = load_settings()
    
    assert settings.llm.default_model == "glm-4.7"
    assert settings.container_pool.pool_size == 5
    assert settings.container_pool.base_port == 9001
    assert len(settings.agent_cards) >= 1


def test_load_custom_config():
    """从自定义 YAML 加载配置"""
    yaml_content = """
server:
  host: "127.0.0.1"
  port: 9000

llm:
  default_model: "custom-model"
  default_base_url: "https://custom.api/v4"
  default_api_key: "custom-key"

container_pool:
  pool_size: 10
  base_port: 8000

agent_cards:
  - id: "test-agent"
    name: "Test Agent"
    description: "A test agent"
    skills:
      - id: "test-skill"
        name: "Test"
        description: "Test skill"
        tags: ["test"]
"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(yaml_content)
        f.flush()
        
        from src.config import load_settings
        settings = load_settings(f.name)
        
        assert settings.server.port == 9000
        assert settings.llm.default_model == "custom-model"
        assert settings.llm.default_api_key == "custom-key"
        assert settings.container_pool.pool_size == 10
        assert settings.container_pool.base_port == 8000
        assert len(settings.agent_cards) == 1
        assert settings.agent_cards[0].id == "test-agent"
        assert settings.agent_cards[0].skills[0]["tags"] == ["test"]
        
        os.unlink(f.name)


def test_load_nonexistent_config():
    """加载不存在的配置文件返回默认值"""
    from src.config import load_settings
    settings = load_settings("/nonexistent/path.yaml")
    
    assert settings.llm.default_model == "glm-coding-plan"
    assert settings.container_pool.pool_size == 5


def test_agent_card_definitions():
    """Agent Card 定义完整"""
    from src.config import load_settings
    settings = load_settings()
    
    ids = [ac.id for ac in settings.agent_cards]
    assert "frontend-ux-pro" in ids
    assert "backend-engineer" in ids
    assert "general-agent" in ids
    
    frontend = next(ac for ac in settings.agent_cards if ac.id == "frontend-ux-pro")
    assert "frontend" in frontend.name.lower()
    assert len(frontend.skills) >= 1
    assert any("ui" in s.get("tags", []) for s in frontend.skills)
