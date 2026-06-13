"""Redis-backed Agent Registry — async implementation.

Agents register, heartbeat, and deregister through this module.  All state is
kept in Redis keys under the ``agent:registry:`` prefix.  Skills are indexed in
Redis SETs (``skills:index:<skill_name>``) for fast lookups, and lifecycle events
are published to Pub/Sub channels (``agent:online:<agent_id>`` /
``agent:offline:<agent_id>``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Callable, Coroutine

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------
_KEY_PREFIX = "agent:registry:"
_SKILL_PREFIX = "skills:index:"


def _agent_key(agent_id: str) -> str:
    return f"{_KEY_PREFIX}{agent_id}"


def _skill_key(skill: str) -> str:
    return f"{_SKILL_PREFIX}{skill}"


# ---------------------------------------------------------------------------
# AgentRegistry
# ---------------------------------------------------------------------------

PubSubCallback = Callable[[str, dict], Coroutine[Any, Any, None]]


class AgentRegistry:
    """Async Redis registry for swarm agents."""

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        heartbeat_ttl: int = 30,
        heartbeat_interval: int = 10,
    ) -> None:
        self._redis_url = redis_url
        self._heartbeat_ttl = heartbeat_ttl
        self._heartbeat_interval = heartbeat_interval

        # Created lazily or via connect()
        self._redis: aioredis.Redis | None = None
        self._pubsub: aioredis.client.PubSub | None = None
        self._subscriber_task: asyncio.Task | None = None
        self._closed = False

    # ---- connection management -------------------------------------------

    async def connect(self) -> None:
        """Explicitly open the Redis connection.  Idempotent."""
        if self._redis is not None:
            return
        self._redis = aioredis.from_url(
            self._redis_url,
            decode_responses=True,
        )
        try:
            await self._redis.ping()
        except Exception:
            logger.warning("Redis ping failed during connect()", exc_info=True)
            self._redis = None
            raise

    async def _ensure_redis(self) -> aioredis.Redis:
        """Return the Redis client, connecting lazily if needed."""
        if self._redis is None:
            await self.connect()
        assert self._redis is not None  # for type-checkers
        return self._redis

    # ---- registration ----------------------------------------------------

    async def register(self, agent_data: dict) -> str:
        """Register a new agent.

        Parameters
        ----------
        agent_data:
            Dict with keys matching :class:`AgentRegistration` (``name``,
            ``endpoint``, ``skills``, etc.).  Extra keys are preserved.

        Returns
        -------
        str
            The generated ``agent_id``.
        """
        from src.registry.models import AgentInfo  # avoid circular at import-time

        redis = await self._ensure_redis()

        agent_id = uuid.uuid4().hex[:16]
        now = time.time()

        # Merge defaults — callers may omit some fields
        agent_data.setdefault("version", "0.1.0")
        agent_data.setdefault("protocol", "http")
        agent_data.setdefault("skills", [])
        agent_data.setdefault("capabilities", {})

        record: dict[str, Any] = {
            "id": agent_id,
            "name": agent_data["name"],
            "endpoint": agent_data["endpoint"],
            "protocol": agent_data["protocol"],
            "skills": agent_data["skills"],
            "capabilities": agent_data["capabilities"],
            "version": agent_data["version"],
            "status": "online",
            "registered_at": now,
            "last_heartbeat": now,
            "instance_id": uuid.uuid4().hex,
        }

        key = _agent_key(agent_id)

        try:
            pipeline = redis.pipeline()
            # Store agent record as JSON hash
            pipeline.hset(key, mapping={"data": json.dumps(record)})
            # Set TTL so stale entries auto-expire
            pipeline.expire(key, self._heartbeat_ttl)

            # Add agent_id to each skill index SET
            for skill in agent_data["skills"]:
                pipeline.sadd(_skill_key(skill), agent_id)
                pipeline.expire(_skill_key(skill), self._heartbeat_ttl * 2)

            await pipeline.execute()

            # Publish online event
            await redis.publish(
                f"agent:online:{agent_id}",
                json.dumps({"agent_id": agent_id, "name": record["name"], "timestamp": now}),
            )

            logger.info("Registered agent %s (%s)", agent_id, record["name"])
            return agent_id

        except Exception:
            logger.warning("Failed to register agent", exc_info=True)
            raise

    # ---- heartbeat --------------------------------------------------------

    async def heartbeat(self, agent_id: str) -> bool:
        """Renew TTL and update ``last_heartbeat``.  Returns *False* if the
        agent is not registered."""
        redis = await self._ensure_redis()
        key = _agent_key(agent_id)

        try:
            raw = await redis.hget(key, "data")
            if raw is None:
                return False

            record = json.loads(raw)
            now = time.time()
            record["last_heartbeat"] = now

            pipeline = redis.pipeline()
            pipeline.hset(key, mapping={"data": json.dumps(record)})
            pipeline.expire(key, self._heartbeat_ttl)
            # Also refresh skill SET TTLs
            for skill in record.get("skills", []):
                pipeline.expire(_skill_key(skill), self._heartbeat_ttl * 2)
            await pipeline.execute()

            return True

        except Exception:
            logger.warning("Heartbeat failed for agent %s", agent_id, exc_info=True)
            return False

    # ---- deregistration --------------------------------------------------

    async def deregister(self, agent_id: str) -> None:
        """Remove an agent and its skill index entries."""
        redis = await self._ensure_redis()
        key = _agent_key(agent_id)

        try:
            raw = await redis.hget(key, "data")
            skills: list[str] = []
            if raw:
                record = json.loads(raw)
                skills = record.get("skills", [])

            pipeline = redis.pipeline()
            pipeline.delete(key)
            for skill in skills:
                pipeline.srem(_skill_key(skill), agent_id)
            await pipeline.execute()

            await redis.publish(
                f"agent:offline:{agent_id}",
                json.dumps({"agent_id": agent_id, "timestamp": time.time()}),
            )

            logger.info("Deregistered agent %s", agent_id)

        except Exception:
            logger.warning("Deregister failed for agent %s", agent_id, exc_info=True)

    # ---- queries ---------------------------------------------------------

    async def get_agent(self, agent_id: str) -> dict | None:
        """Return the full agent record dict, or *None*."""
        redis = await self._ensure_redis()
        try:
            raw = await redis.hget(_agent_key(agent_id), "data")
            if raw is None:
                return None
            return json.loads(raw)
        except Exception:
            logger.warning("get_agent failed for %s", agent_id, exc_info=True)
            return None

    async def list_agents(self) -> list[dict]:
        """Return all registered agents."""
        redis = await self._ensure_redis()
        try:
            keys = await redis.keys(f"{_KEY_PREFIX}*")
            if not keys:
                return []

            # Pipeline fetch
            pipe = redis.pipeline()
            for k in keys:
                pipe.hget(k, "data")
            results = await pipe.execute()

            agents: list[dict] = []
            for raw in results:
                if raw is not None:
                    agents.append(json.loads(raw))
            return agents

        except Exception:
            logger.warning("list_agents failed", exc_info=True)
            return []

    async def find_by_skill(self, skill: str) -> list[dict]:
        """Find agents that declare *skill*."""
        redis = await self._ensure_redis()
        try:
            agent_ids = await redis.smembers(_skill_key(skill))
            if not agent_ids:
                return []

            pipe = redis.pipeline()
            for aid in agent_ids:
                pipe.hget(_agent_key(aid), "data")
            results = await pipe.execute()

            agents: list[dict] = []
            for raw in results:
                if raw is not None:
                    agents.append(json.loads(raw))
            return agents

        except Exception:
            logger.warning("find_by_skill(%s) failed", skill, exc_info=True)
            return []

    # ---- pub/sub ---------------------------------------------------------

    async def subscribe(self, callback: PubSubCallback) -> None:
        """Subscribe to ``agent:online:*`` and ``agent:offline:*`` channels.

        The *callback* receives ``(event_type, payload_dict)`` where
        ``event_type`` is ``"online"`` or ``"offline"``.
        """
        redis = await self._ensure_redis()

        self._pubsub = redis.pubsub()
        # Redis pubsub does not support glob natively in subscribe;
        # use pattern subscribe.
        await self._pubsub.psubscribe("agent:online:*", "agent:offline:*")

        async def _listener() -> None:
            assert self._pubsub is not None
            try:
                async for message in self._pubsub.listen():
                    if message["type"] != "pmessage":
                        continue

                    channel = message["channel"]
                    try:
                        payload = json.loads(message["data"])
                    except (json.JSONDecodeError, TypeError):
                        payload = {"raw": message["data"]}

                    if channel.startswith("agent:online:"):
                        event_type = "online"
                    elif channel.startswith("agent:offline:"):
                        event_type = "offline"
                    else:
                        continue

                    try:
                        await callback(event_type, payload)
                    except Exception:
                        logger.warning("Pub/Sub callback error", exc_info=True)

            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("Pub/Sub listener exited unexpectedly", exc_info=True)

        self._subscriber_task = asyncio.create_task(_listener())
        logger.info("Pub/Sub subscriber started")

    # ---- health sweep ----------------------------------------------------

    async def health_sweep(self) -> int:
        """Remove agents whose key has expired (stale cleanup).

        Since we set TTL on agent keys, Redis already removes the data.  This
        method additionally cleans up any orphaned skill SET entries.

        Returns the number of agents swept (keys that are already gone but
        still referenced in skill indexes).
        """
        redis = await self._ensure_redis()
        swept = 0
        try:
            # Scan all skill index keys
            skill_keys: list[str] = []
            async for key in redis.scan_iter(f"{_SKILL_PREFIX}*"):
                skill_keys.append(key)

            for sk in skill_keys:
                members = await redis.smembers(sk)
                to_remove: list[str] = []
                for agent_id in members:
                    exists = await redis.exists(_agent_key(agent_id))
                    if not exists:
                        to_remove.append(agent_id)
                        swept += 1
                if to_remove:
                    await redis.srem(sk, *to_remove)

            if swept:
                logger.info("Health sweep removed %d orphaned skill references", swept)

        except Exception:
            logger.warning("health_sweep failed", exc_info=True)

        return swept

    # ---- cleanup ---------------------------------------------------------

    async def close(self) -> None:
        """Close Redis connections and cancel subscriber task."""
        if self._closed:
            return
        self._closed = True

        if self._subscriber_task and not self._subscriber_task.done():
            self._subscriber_task.cancel()
            try:
                await self._subscriber_task
            except asyncio.CancelledError:
                pass
            self._subscriber_task = None

        if self._pubsub:
            try:
                await self._pubsub.punsubscribe()
                await self._pubsub.aclose()
            except Exception:
                logger.warning("Error closing pubsub", exc_info=True)
            self._pubsub = None

        if self._redis:
            try:
                await self._redis.aclose()
            except Exception:
                logger.warning("Error closing Redis client", exc_info=True)
            self._redis = None

        logger.info("AgentRegistry closed")
