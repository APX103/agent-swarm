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


def test_env_overrides_apply_when_nonempty(monkeypatch):
    """A3: non-empty env vars override yaml config (needed for container deploy)."""
    from src.config import load_settings

    monkeypatch.setenv("LLM_DEFAULT_MODEL", "env-model")
    monkeypatch.setenv("SHARED_OUTPUT_BASE", "/env/path")
    monkeypatch.setenv("CONTAINER_WORKER_HOST", "host.docker.internal")
    monkeypatch.setenv("CONTAINER_POOL_SIZE", "7")
    s = load_settings()
    assert s.llm.default_model == "env-model"
    assert s.storage.shared_output_base == "/env/path"
    assert s.container_pool.worker_host == "host.docker.internal"
    assert s.container_pool.pool_size == 7


def test_env_empty_does_not_override_yaml(monkeypatch):
    """A3: empty env must not clobber the yaml value."""
    from src.config import load_settings

    monkeypatch.setenv("LLM_DEFAULT_MODEL", "")
    s = load_settings()
    assert s.llm.default_model == "glm-4.7"
