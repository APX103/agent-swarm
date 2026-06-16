"""Comprehensive tests for the Agent Registry."""
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# FakeRedis — in-memory store that mimics redis.asyncio interface
# ---------------------------------------------------------------------------

class FakePipeline:
    """Accumulates commands and executes them in batch."""

    def __init__(self, store: dict):
        self._store = store
        self._commands: list = []

    # --- commands that queue up ---

    def set(self, key, value, **kwargs):
        self._commands.append(("set", key, value))
        return self

    def get(self, key):
        self._commands.append(("get", key))
        return self

    def hset(self, key, mapping=None, **kwargs):
        self._commands.append(("hset", key, mapping or {}))
        return self

    def expire(self, key, seconds):
        self._commands.append(("expire", key, seconds))
        return self

    def sadd(self, key, *values):
        self._commands.append(("sadd", key, values))
        return self

    def srem(self, key, *values):
        self._commands.append(("srem", key, values))
        return self

    def delete(self, *keys):
        self._commands.append(("delete", keys))
        return self

    def hget(self, key, field):
        self._commands.append(("hget", key, field))
        return self

    # --- execute ---

    async def execute(self):
        results = []
        for cmd in self._commands:
            op = cmd[0]
            if op == "set":
                _key, value = cmd[1], cmd[2]
                self._store[_key] = value
                results.append(True)
            elif op == "get":
                _key = cmd[1]
                results.append(self._store.get(_key))
            elif op == "hset":
                _key, mapping = cmd[1], cmd[2]
                self._store.setdefault(_key, {}).update(mapping)
                results.append(True)
            elif op == "expire":
                _key = cmd[1]
                self._store.setdefault(_key, {})
                # Store TTL metadata; not enforced in fake but tracked
                self._store[f"__ttl:{cmd[1]}"] = cmd[2]
                results.append(True)
            elif op == "sadd":
                _key, values = cmd[1], cmd[2]
                entry = self._store.setdefault(_key, {"__type": "set", "members": set()})
                entry["members"].update(values)
                results.append(len(values))
            elif op == "srem":
                _key, values = cmd[1], cmd[2]
                entry = self._store.get(_key)
                if entry:
                    entry["members"] -= set(values)
                results.append(1)
            elif op == "delete":
                keys = cmd[1]
                for k in keys:
                    self._store.pop(k, None)
                results.append(len(keys))
            elif op == "hget":
                _key, field = cmd[1], cmd[2]
                entry = self._store.get(_key, {})
                results.append(entry.get(field) if isinstance(entry, dict) and "data" in entry else None)
        self._commands.clear()
        return results


class FakeRedis:
    """Minimal async Redis fake backed by a plain dict."""

    def __init__(self, store: dict | None = None):
        self._store = store if store is not None else {}

    # --- connection ---

    async def ping(self):
        return True

    async def aclose(self):
        pass

    # --- strings / hashes ---

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, **kwargs):
        self._store[key] = value

    async def hset(self, key, mapping=None, **kwargs):
        self._store.setdefault(key, {}).update(mapping or {})
        return len(mapping or {})

    async def hget(self, key, field):
        entry = self._store.get(key)
        if isinstance(entry, dict):
            return entry.get(field)
        return None

    # --- sets ---

    async def sadd(self, key, *values):
        entry = self._store.setdefault(key, {"__type": "set", "members": set()})
        entry["members"].update(values)
        return len(values)

    async def srem(self, key, *values):
        entry = self._store.get(key)
        if entry and isinstance(entry, dict):
            entry["members"] -= set(values)
        return 1

    async def smembers(self, key):
        entry = self._store.get(key)
        if entry and isinstance(entry, dict) and "members" in entry:
            return entry["members"]
        return set()

    # --- ttl ---

    async def expire(self, key, seconds):
        self._store[f"__ttl:{key}"] = seconds
        return True

    # --- key scanning ---

    async def keys(self, pattern: str):
        """Very basic glob: only supports trailing ``*``."""
        prefix = pattern.rstrip("*")
        return [k for k in self._store if k.startswith(prefix) and not k.startswith("__ttl:")]

    async def exists(self, key) -> int:
        return 1 if key in self._store else 0

    async def delete(self, *keys):
        for k in keys:
            self._store.pop(k, None)
        return len(keys)

    # --- pubsub ---

    async def publish(self, channel: str, message: str):
        self._store.setdefault(f"__pub:{channel}", []).append(message)
        return 1

    # --- pipeline ---

    def pipeline(self):
        return FakePipeline(self._store)

    # --- scan_iter ---

    def scan_iter(self, match: str):
        prefix = match.rstrip("*")
        matched = [k for k in self._store if k.startswith(prefix) and not k.startswith("__ttl:")]

        async def _gen():
            for k in matched:
                yield k

        return _gen()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_store():
    """Shared dict that the FakeRedis instance writes to."""
    return {}


@pytest.fixture
def fake_redis(fake_store):
    return FakeRedis(fake_store)


@pytest.fixture
def registry():
    from src.registry.registry import AgentRegistry
    return AgentRegistry(
        redis_url="redis://localhost:6379/0",
        heartbeat_ttl=30,
        heartbeat_interval=10,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _connect_registry(reg, fake_redis):
    """Manually set the redis client on the registry (bypass connect)."""
    reg._redis = fake_redis


def _sample_agent_data(**overrides):
    """Return a valid agent-data dict for register()."""
    data = {
        "name": "test-agent",
        "endpoint": "http://localhost:8000",
        "protocol": "http",
        "skills": ["nlp", "translation"],
        "capabilities": {"input_modes": ["text"], "output_modes": ["text"], "tools": []},
        "version": "1.0.0",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@patch("src.registry.registry.aioredis.from_url")
def test_registry_initialization(mock_from_url):
    """Registry is created with the supplied config."""
    from src.registry.registry import AgentRegistry

    reg = AgentRegistry(
        redis_url="redis://custom:6380/1",
        heartbeat_ttl=60,
        heartbeat_interval=20,
    )

    assert reg._redis_url == "redis://custom:6380/1"
    assert reg._heartbeat_ttl == 60
    assert reg._heartbeat_interval == 20
    assert reg._redis is None  # not connected yet
    assert reg._closed is False


@pytest.mark.asyncio
async def test_register_agent(registry, fake_store, fake_redis):
    """register() returns an agent_id, stores data in Redis, sets TTL, publishes online."""
    await _connect_registry(registry, fake_redis)

    agent_data = _sample_agent_data()
    agent_id = await registry.register(agent_data)

    # Returns a 16-char hex id
    assert isinstance(agent_id, str)
    assert len(agent_id) == 16

    # Data stored in hash
    key = f"agent:registry:{agent_id}"
    raw = fake_store[key]["data"]
    record = json.loads(raw)

    assert record["name"] == "test-agent"
    assert record["endpoint"] == "http://localhost:8000"
    assert record["status"] == "online"
    assert "registered_at" in record
    assert "last_heartbeat" in record
    assert record["id"] == agent_id

    # TTL set
    assert f"__ttl:agent:registry:{agent_id}" in fake_store

    # Online event published
    pub_key = f"__pub:agent:online:{agent_id}"
    assert pub_key in fake_store
    event = json.loads(fake_store[pub_key][0])
    assert event["agent_id"] == agent_id
    assert event["name"] == "test-agent"


@pytest.mark.asyncio
async def test_register_agent_with_skills(registry, fake_store, fake_redis):
    """Skills are indexed in Redis SETs."""
    await _connect_registry(registry, fake_redis)

    agent_data = _sample_agent_data(skills=["nlp", "translation", "summarization"])
    agent_id = await registry.register(agent_data)

    # Each skill should have its own SET containing the agent_id
    for skill in ["nlp", "translation", "summarization"]:
        skill_key = f"skills:index:{skill}"
        members = fake_store[skill_key]["members"]
        assert agent_id in members

    # Skill SETs should also have a TTL
    for skill in ["nlp", "translation", "summarization"]:
        ttl_key = f"__ttl:skills:index:{skill}"
        assert ttl_key in fake_store


@pytest.mark.asyncio
async def test_heartbeat_renews_ttl(registry, fake_store, fake_redis):
    """heartbeat() updates last_heartbeat and resets TTL."""
    await _connect_registry(registry, fake_redis)

    agent_id = await registry.register(_sample_agent_data())
    key = f"agent:registry:{agent_id}"

    # Capture time before heartbeat
    await asyncio.sleep(0.01)
    result = await registry.heartbeat(agent_id)

    assert result is True

    # Verify last_heartbeat was updated
    raw = fake_store[key]["data"]
    record = json.loads(raw)
    assert "last_heartbeat" in record

    # TTL should be refreshed
    assert f"__ttl:{key}" in fake_store


@pytest.mark.asyncio
async def test_heartbeat_unknown_agent(registry, fake_redis):
    """heartbeat() returns False for an unknown agent_id."""
    await _connect_registry(registry, fake_redis)

    result = await registry.heartbeat("nonexistent_agent_id")
    assert result is False


@pytest.mark.asyncio
async def test_deregister_agent(registry, fake_store, fake_redis):
    """deregister() removes the agent key and its skill index entries, publishes offline."""
    await _connect_registry(registry, fake_redis)

    agent_data = _sample_agent_data(skills=["nlp", "translation"])
    agent_id = await registry.register(agent_data)
    key = f"agent:registry:{agent_id}"

    # Confirm it's there
    assert key in fake_store

    await registry.deregister(agent_id)

    # Agent key removed
    assert key not in fake_store

    # Skill SET entries removed
    for skill in ["nlp", "translation"]:
        skill_key = f"skills:index:{skill}"
        if skill_key in fake_store:
            members = fake_store[skill_key]["members"]
            assert agent_id not in members

    # Offline event published
    pub_key = f"__pub:agent:offline:{agent_id}"
    assert pub_key in fake_store
    event = json.loads(fake_store[pub_key][0])
    assert event["agent_id"] == agent_id


@pytest.mark.asyncio
async def test_get_agent_exists(registry, fake_redis):
    """get_agent() returns the agent record dict when it exists."""
    await _connect_registry(registry, fake_redis)

    agent_id = await registry.register(_sample_agent_data())
    agent = await registry.get_agent(agent_id)

    assert agent is not None
    assert agent["id"] == agent_id
    assert agent["name"] == "test-agent"
    assert agent["endpoint"] == "http://localhost:8000"
    assert agent["skills"] == ["nlp", "translation"]
    assert agent["status"] == "online"


@pytest.mark.asyncio
async def test_get_agent_not_found(registry, fake_redis):
    """get_agent() returns None when agent does not exist."""
    await _connect_registry(registry, fake_redis)

    agent = await registry.get_agent("nonexistent")
    assert agent is None


@pytest.mark.asyncio
async def test_list_agents(registry, fake_redis):
    """list_agents() returns all registered agents."""
    await _connect_registry(registry, fake_redis)

    id1 = await registry.register(_sample_agent_data(name="agent-one", endpoint="http://localhost:8001"))
    id2 = await registry.register(_sample_agent_data(name="agent-two", endpoint="http://localhost:8002"))
    id3 = await registry.register(_sample_agent_data(name="agent-three", endpoint="http://localhost:8003"))

    agents = await registry.list_agents()

    assert len(agents) == 3
    names = {a["name"] for a in agents}
    assert names == {"agent-one", "agent-two", "agent-three"}

    # All should have valid IDs
    returned_ids = {a["id"] for a in agents}
    assert returned_ids == {id1, id2, id3}


@pytest.mark.asyncio
async def test_list_agents_empty(registry, fake_redis):
    """list_agents() returns empty list when no agents are registered."""
    await _connect_registry(registry, fake_redis)

    agents = await registry.list_agents()
    assert agents == []


@pytest.mark.asyncio
async def test_find_by_skill(registry, fake_redis):
    """find_by_skill() returns agents that have the specified skill."""
    await _connect_registry(registry, fake_redis)

    await registry.register(_sample_agent_data(name="nlp-agent", skills=["nlp", "translation"], endpoint="http://a:8000"))
    await registry.register(_sample_agent_data(name="code-agent", skills=["coding", "debugging"], endpoint="http://b:8000"))
    await registry.register(_sample_agent_data(name="multi-agent", skills=["nlp", "coding"], endpoint="http://c:8000"))

    nlp_agents = await registry.find_by_skill("nlp")
    assert len(nlp_agents) == 2
    names = {a["name"] for a in nlp_agents}
    assert names == {"nlp-agent", "multi-agent"}

    coding_agents = await registry.find_by_skill("coding")
    assert len(coding_agents) == 2
    names = {a["name"] for a in coding_agents}
    assert names == {"code-agent", "multi-agent"}

    translation_agents = await registry.find_by_skill("translation")
    assert len(translation_agents) == 1
    assert translation_agents[0]["name"] == "nlp-agent"


@pytest.mark.asyncio
async def test_find_by_skill_no_match(registry, fake_redis):
    """find_by_skill() returns empty list when no agent has the skill."""
    await _connect_registry(registry, fake_redis)

    await registry.register(_sample_agent_data(skills=["nlp", "translation"]))

    result = await registry.find_by_skill("nonexistent_skill")
    assert result == []


@pytest.mark.asyncio
async def test_health_sweep(registry, fake_store, fake_redis):
    """health_sweep() cleans up orphaned skill index entries for expired agents."""
    await _connect_registry(registry, fake_redis)

    agent_id = await registry.register(_sample_agent_data(skills=["nlp", "coding"]))

    # Manually delete the agent key (simulating TTL expiry) but leave skill SETs
    key = f"agent:registry:{agent_id}"
    del fake_store[key]

    # Confirm agent key is gone but skill SETs still reference it
    assert key not in fake_store
    skill_key = "skills:index:nlp"
    assert agent_id in fake_store[skill_key]["members"]

    swept = await registry.health_sweep()

    assert swept >= 1

    # Orphaned references should be cleaned from skill SETs
    if skill_key in fake_store:
        assert agent_id not in fake_store[skill_key]["members"]


@pytest.mark.asyncio
async def test_health_sweep_no_orphans(registry, fake_redis):
    """health_sweep() returns 0 when no orphaned entries exist."""
    await _connect_registry(registry, fake_redis)

    await registry.register(_sample_agent_data(skills=["nlp"]))

    swept = await registry.health_sweep()
    assert swept == 0


@pytest.mark.asyncio
@patch("src.registry.registry.aioredis.from_url")
async def test_register_without_redis(mock_from_url):
    """register() raises gracefully when Redis is unavailable."""
    from src.registry.registry import AgentRegistry

    # Make from_url raise a connection error
    mock_redis = AsyncMock()
    mock_redis.ping.side_effect = ConnectionError("Redis not available")
    mock_from_url.return_value = mock_redis

    reg = AgentRegistry(redis_url="redis://localhost:6379/0")

    with pytest.raises(ConnectionError):
        await reg.register(_sample_agent_data())

    mock_from_url.assert_called_once()


@pytest.mark.asyncio
@patch("src.registry.registry.aioredis.from_url")
async def test_connect_idempotent(mock_from_url):
    """Calling connect() twice only creates one Redis connection."""
    from src.registry.registry import AgentRegistry

    mock_redis = AsyncMock()
    mock_redis.ping.return_value = True
    mock_from_url.return_value = mock_redis

    reg = AgentRegistry()
    await reg.connect()
    await reg.connect()  # second call should be no-op

    mock_from_url.assert_called_once()


@pytest.mark.asyncio
async def test_register_default_values(registry, fake_redis):
    """register() applies sensible defaults for omitted fields."""
    await _connect_registry(registry, fake_redis)

    minimal_data = {"name": "minimal", "endpoint": "http://localhost:9000"}
    agent_id = await registry.register(minimal_data)

    agent = await registry.get_agent(agent_id)
    assert agent["version"] == "0.1.0"
    assert agent["protocol"] == "http"
    assert agent["skills"] == []
    assert agent["capabilities"] == {}
    assert agent["status"] == "online"


@pytest.mark.asyncio
async def test_heartbeat_updates_last_heartbeat_value(registry, fake_redis):
    """heartbeat() sets last_heartbeat to a recent timestamp."""
    await _connect_registry(registry, fake_redis)

    agent_id = await registry.register(_sample_agent_data())

    # Get the initial last_heartbeat
    agent_before = await registry.get_agent(agent_id)
    t_before = agent_before["last_heartbeat"]

    # Small sleep to ensure time difference
    await asyncio.sleep(0.02)

    await registry.heartbeat(agent_id)

    agent_after = await registry.get_agent(agent_id)
    t_after = agent_after["last_heartbeat"]

    assert t_after > t_before


@pytest.mark.asyncio
async def test_deregister_nonexistent_agent(registry, fake_redis):
    """deregister() handles unknown agent gracefully (no crash)."""
    await _connect_registry(registry, fake_redis)

    # Should not raise
    await registry.deregister("ghost_agent_id")


@pytest.mark.asyncio
async def test_close_cleans_up(registry, fake_redis):
    """close() resets internal state without errors."""
    await _connect_registry(registry, fake_redis)

    # Register something to prove redis is active
    await registry.register(_sample_agent_data())

    await registry.close()

    assert registry._redis is None
    assert registry._closed is True
