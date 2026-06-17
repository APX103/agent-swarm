import asyncio
import logging
from typing import Any, Optional

import httpx

from .base import AgentBackend, AgentCapabilities, AgentResult, ProgressCallback

logger = logging.getLogger(__name__)


class MCPAdapter(AgentBackend):
    """Adapter for MCP (Model Context Protocol) servers via JSON-RPC."""

    def __init__(
        self,
        server_url: str,
        timeout: int = 30,
        **kwargs,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._client_lock = asyncio.Lock()
        self._request_id = 0

    async def _get_client(self) -> httpx.AsyncClient:
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    base_url=self.server_url,
                    headers={"Content-Type": "application/json"},
                    timeout=httpx.Timeout(self.timeout),
                )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _jsonrpc(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "method": method,
            "id": self._next_id(),
            "params": params or {},
        }

    @property
    def name(self) -> str:
        return f"mcp:{self.server_url}"

    @property
    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            skills=["tools"],
            input_modes=["text"],
            output_modes=["text", "json"],
        )

    async def invoke(
        self, task: str, context: dict = None, on_progress: Optional[ProgressCallback] = None
    ) -> AgentResult:
        """Send a tools/call JSON-RPC request to the MCP server.

        MCP tool calls are request/response; *on_progress* is accepted for
        interface compatibility but ignored.
        """
        client = await self._get_client()

        tool_name = (context or {}).get("tool_name", "default")
        payload = self._jsonrpc(
            method="tools/call",
            params={
                "name": tool_name,
                "arguments": {"task": task, **((context or {}).get("tool_args", {}))},
            },
        )

        try:
            response = await client.post("/", json=payload)
            response.raise_for_status()
            data = response.json()

            if "error" in data:
                error_info = data["error"]
                logger.error("MCP JSON-RPC error: %s", error_info)
                return AgentResult(
                    success=False,
                    output="",
                    error=f"JSON-RPC error {error_info.get('code', '?')}: {error_info.get('message', 'unknown')}",
                    metadata={"rpc_error": error_info},
                )

            result = data.get("result", {})
            content_list = result.get("content", [])
            is_error = result.get("isError", False)

            if is_error:
                text_parts = [
                    item.get("text", "")
                    for item in content_list
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                error_text = "\n".join(text_parts)
                return AgentResult(
                    success=False,
                    output="",
                    error=error_text or "MCP tool returned error",
                    metadata={"mcp_result": result},
                )

            # Extract text content from MCP response
            text_parts = []
            artifacts: list[str] = []
            for item in content_list:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                    elif item.get("type") == "resource":
                        artifacts.append(str(item.get("resource", item)))

            return AgentResult(
                success=True,
                output="\n".join(text_parts) if text_parts else "",
                artifacts=artifacts,
                metadata={"mcp_result": result},
            )

        except httpx.HTTPStatusError as e:
            logger.error("MCP HTTP error: %s %s", e.response.status_code, e.response.text)
            return AgentResult(
                success=False,
                output="",
                error=f"HTTP {e.response.status_code}: {e.response.text}",
                metadata={"status_code": e.response.status_code},
            )
        except httpx.RequestError as e:
            logger.error("MCP request error: %s", e)
            return AgentResult(
                success=False,
                output="",
                error=f"Request error: {e!s}",
            )
        except Exception as e:
            logger.exception("MCP adapter unexpected error")
            return AgentResult(
                success=False,
                output="",
                error=f"Unexpected error: {e!s}",
            )

    async def health_check(self) -> bool:
        """Check MCP server health by sending a tools/list request."""
        client = await self._get_client()
        payload = self._jsonrpc(method="tools/list")

        try:
            response = await client.post("/", json=payload)
            if response.status_code != 200:
                return False
            data = response.json()
            # Valid JSON-RPC response should not contain an error key (or error should be null)
            return "error" not in data or data["error"] is None
        except Exception:
            logger.debug("MCP health check failed for %s", self.server_url)
            return False
