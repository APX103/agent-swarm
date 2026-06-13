"""Async HTTP client for agents to self-register with the swarm gateway."""
import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


class AgentClient:
    """Client for an agent to register, heartbeat, and deregister with the gateway."""

    def __init__(self, gateway_url: str):
        self._gateway_url = gateway_url.rstrip("/")
        self._http: httpx.AsyncClient | None = None
        self._agent_id: str | None = None

    # ── public API ────────────────────────────────────────────────────────────

    async def register(
        self,
        name: str,
        endpoint: str,
        protocol: str = "http",
        skills: list[str] | None = None,
        heartbeat_interval: int = 10,
    ) -> str:
        """Register this agent with the gateway. Returns the assigned agent_id."""
        async with self._client() as http:
            resp = await http.post(
                f"{self._gateway_url}/api/v1/agents/register",
                json={
                    "name": name,
                    "endpoint": endpoint,
                    "protocol": protocol,
                    "skills": skills or [],
                    "heartbeat_interval": heartbeat_interval,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._agent_id = data["agent_id"]
            logger.info("Registered as agent_id=%s", self._agent_id)
            return self._agent_id

    async def heartbeat(self, agent_id: str) -> bool:
        """Send a single heartbeat for *agent_id*. Returns True on success."""
        try:
            async with self._client() as http:
                resp = await http.post(
                    f"{self._gateway_url}/api/v1/agents/{agent_id}/heartbeat",
                )
                resp.raise_for_status()
                return True
        except Exception:
            logger.warning("Heartbeat failed for agent_id=%s", agent_id, exc_info=True)
            return False

    async def deregister(self) -> None:
        """Deregister the agent (if registered)."""
        if not self._agent_id:
            return
        try:
            async with self._client() as http:
                resp = await http.post(
                    f"{self._gateway_url}/api/v1/agents/{self._agent_id}/deregister",
                )
                resp.raise_for_status()
                logger.info("Deregistered agent_id=%s", self._agent_id)
        except Exception:
            logger.warning("Deregister failed for agent_id=%s", self._agent_id, exc_info=True)
        finally:
            self._agent_id = None

    # ── async context manager ────────────────────────────────────────────────

    async def __aenter__(self) -> "AgentClient":
        return self

    async def __aexit__(self, *args):
        await self.deregister()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=10)


# ── heartbeat loop helper ─────────────────────────────────────────────────────


async def start_heartbeat_loop(
    client: AgentClient,
    agent_id: str,
    interval: int = 10,
) -> None:
    """Background task that sends heartbeats every *interval* seconds.

    Stops when cancelled (e.g. via ``asyncio.Task.cancel()``).
    """
    logger.info("Heartbeat loop started for agent_id=%s (interval=%ds)", agent_id, interval)
    try:
        while True:
            await asyncio.sleep(interval)
            ok = await client.heartbeat(agent_id)
            if not ok:
                logger.warning("Heartbeat lost for agent_id=%s", agent_id)
    except asyncio.CancelledError:
        logger.info("Heartbeat loop cancelled for agent_id=%s", agent_id)
        raise
