"""Wave 6 tests: startup config self-check (fail-fast / defensive warnings)."""
from src.config import Settings, LLMConfig, ContainerPoolConfig, StorageConfig, validate_settings
from src.orchestrator.base import OrchestratorConfig


def _good() -> Settings:
    return Settings(
        llm=LLMConfig(default_api_key="k", default_base_url="https://x/v4"),
        container_pool=ContainerPoolConfig(pool_size=2),
        storage=StorageConfig(shared_output_base="/tmp/x"),
    )


def test_validate_clean_config_has_no_warnings():
    assert validate_settings(_good()) == []


def test_validate_missing_api_key():
    s = _good()
    s.llm.default_api_key = ""
    warns = validate_settings(s)
    assert any("api_key" in w for w in warns)


def test_validate_external_provider_without_endpoint():
    s = _good()
    s.orchestrator = OrchestratorConfig(provider="external", external_endpoint="")
    warns = validate_settings(s)
    assert any("external_endpoint" in w for w in warns)


def test_validate_zero_pool_size():
    s = _good()
    s.container_pool.pool_size = 0
    warns = validate_settings(s)
    assert any("pool_size" in w for w in warns)
