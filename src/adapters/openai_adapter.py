import logging
from typing import Optional

import httpx

from .base import AgentBackend, AgentCapabilities, AgentResult, ProgressCallback

logger = logging.getLogger(__name__)


class OpenAIAdapter(AgentBackend):
    """Adapter for OpenAI-compatible chat completion APIs."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "default",
        timeout: int = 300,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(self.timeout),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @property
    def name(self) -> str:
        return f"openai:{self.model}@{self.base_url}"

    @property
    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            skills=["chat", "text-generation"],
            input_modes=["text"],
            output_modes=["text"],
        )

    async def invoke(
        self, task: str, context: dict = None, on_progress: Optional[ProgressCallback] = None
    ) -> AgentResult:
        """Send a chat completion request to the OpenAI-compatible API.

        OpenAI chat completions are request/response; *on_progress* is accepted
        for interface compatibility but ignored.
        """
        client = await self._get_client()
        system_msg = context.get("system_prompt", "You are a helpful assistant.") if context else "You are a helpful assistant."

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": task},
            ],
        }

        # Allow context to inject additional parameters
        if context:
            for key in ("temperature", "max_tokens", "top_p", "stream"):
                if key in context:
                    payload[key] = context[key]

        try:
            response = await client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()

            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})

            return AgentResult(
                success=True,
                output=content,
                metadata={
                    "model": data.get("model", self.model),
                    "usage": usage,
                    "finish_reason": data["choices"][0].get("finish_reason"),
                },
            )
        except httpx.HTTPStatusError as e:
            logger.error("OpenAI HTTP error: %s %s", e.response.status_code, e.response.text)
            return AgentResult(
                success=False,
                output="",
                error=f"HTTP {e.response.status_code}: {e.response.text}",
                metadata={"status_code": e.response.status_code},
            )
        except httpx.RequestError as e:
            logger.error("OpenAI request error: %s", e)
            return AgentResult(
                success=False,
                output="",
                error=f"Request error: {e!s}",
            )
        except Exception as e:
            logger.exception("OpenAI adapter unexpected error")
            return AgentResult(
                success=False,
                output="",
                error=f"Unexpected error: {e!s}",
            )

    async def health_check(self) -> bool:
        """Check API health by fetching the models list."""
        client = await self._get_client()
        try:
            resp = await client.get("/v1/models")
            return resp.status_code == 200
        except Exception:
            logger.debug("OpenAI /v1/models health check failed, trying /health")
            try:
                resp = await client.get("/health")
                return resp.status_code == 200
            except Exception:
                logger.debug("OpenAI /health check also failed")
                return False
