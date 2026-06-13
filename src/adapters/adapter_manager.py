import logging
from typing import Optional

from .base import AgentBackend
from .cli_adapter import CLIAdapter
from .mcp_adapter import MCPAdapter
from .openai_adapter import OpenAIAdapter

logger = logging.getLogger(__name__)

# Protocol -> adapter class mapping
PROTOCOL_REGISTRY: dict[str, type[AgentBackend]] = {
    "openai": OpenAIAdapter,
    "cli": CLIAdapter,
    "mcp": MCPAdapter,
    # A2A reuses the OpenAI adapter for now
    "a2a": OpenAIAdapter,
}


def create_adapter(agent_info: dict) -> AgentBackend:
    """Factory method that creates the correct adapter based on protocol field.

    Expected agent_info keys:
        protocol: str - one of "openai", "cli", "mcp", "a2a"
        (plus protocol-specific keys)

    OpenAI-specific:
        base_url: str, api_key: str (optional), model: str (optional), timeout: int (optional)

    CLI-specific:
        command: str, args: list[str] (optional), timeout: int (optional), workdir: str (optional)

    MCP-specific:
        server_url: str, timeout: int (optional)
    """
    protocol = agent_info.get("protocol", "").lower()
    adapter_cls = PROTOCOL_REGISTRY.get(protocol)

    if adapter_cls is None:
        raise ValueError(
            f"Unknown protocol '{protocol}'. "
            f"Supported protocols: {', '.join(PROTOCOL_REGISTRY.keys())}"
        )

    # Extract the adapter-specific config (everything except protocol and metadata)
    config = {k: v for k, v in agent_info.items() if k not in ("protocol", "metadata")}

    try:
        adapter = adapter_cls(**config)
        logger.info("Created %s adapter for agent: %s", protocol, adapter.name)
        return adapter
    except TypeError as e:
        raise ValueError(
            f"Invalid configuration for protocol '{protocol}': {e}"
        ) from e


class AdapterManager:
    """Central registry that maps agent IDs to their backend adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, AgentBackend] = {}

    def register(self, agent_id: str, backend: AgentBackend) -> None:
        """Register a backend adapter for a given agent ID."""
        if agent_id in self._adapters:
            logger.warning("Overwriting existing adapter for agent '%s'", agent_id)
        self._adapters[agent_id] = backend
        logger.info("Registered adapter '%s' for agent '%s'", backend.name, agent_id)

    def get(self, agent_id: str) -> Optional[AgentBackend]:
        """Retrieve the adapter for an agent ID, or None if not registered."""
        return self._adapters.get(agent_id)

    def register_from_info(self, agent_id: str, agent_info: dict) -> AgentBackend:
        """Convenience: create adapter from info dict and register it."""
        adapter = create_adapter(agent_info)
        self.register(agent_id, adapter)
        return adapter

    def unregister(self, agent_id: str) -> Optional[AgentBackend]:
        """Remove and return an adapter, or None if not found."""
        adapter = self._adapters.pop(agent_id, None)
        if adapter:
            logger.info("Unregistered adapter for agent '%s'", agent_id)
        return adapter

    def list_agents(self) -> list[str]:
        """Return all registered agent IDs."""
        return list(self._adapters.keys())

    async def close_all(self) -> None:
        """Close all adapters that support it (e.g., HTTP clients)."""
        for agent_id, adapter in self._adapters.items():
            if hasattr(adapter, "close"):
                try:
                    await adapter.close()
                except Exception:
                    logger.exception("Error closing adapter for agent '%s'", agent_id)
        self._adapters.clear()
        logger.info("All adapters closed and unregistered")
