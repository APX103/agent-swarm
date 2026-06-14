"""Tests for the Agent Adapter system and Circuit Breaker.

Covers OpenAI, CLI, and MCP adapters, AdapterManager, and the resilience
CircuitBreaker.  All external I/O is mocked.
"""
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.base import AgentBackend, AgentCapabilities, AgentResult
from src.adapters.openai_adapter import OpenAIAdapter
from src.adapters.cli_adapter import CLIAdapter
from src.adapters.mcp_adapter import MCPAdapter
from src.adapters.adapter_manager import AdapterManager, create_adapter
from src.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)


# ====================================================================
# OpenAI Adapter
# ====================================================================


class TestOpenAIAdapter:
    """Tests for OpenAIAdapter.invoke, .health_check, and .name."""

    def setup_method(self):
        self.adapter = OpenAIAdapter(
            base_url="http://localhost:8000",
            api_key="test-key",
            model="gpt-4",
            timeout=30,
        )

    # -- helpers --

    def _make_response(self, status_code=200, json_data=None):
        """Build a mock httpx.Response-like object."""
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_data or {}
        resp.text = ""
        resp.raise_for_status.side_effect = (
            None if status_code < 400
            else MagicMock(
                __str__=lambda self: f"HTTP {status_code}",
                response=resp,
            )
        )
        # Simulate httpx.HTTPStatusError when raise_for_status is called
        if status_code >= 400:
            import httpx

            error = httpx.HTTPStatusError(
                f"HTTP {status_code}",
                request=MagicMock(),
                response=resp,
            )
            resp.raise_for_status.side_effect = error
        return resp

    # -- tests --

    @pytest.mark.asyncio
    async def test_openai_adapter_invoke_success(self):
        """A normal 200 response produces a successful AgentResult."""
        json_data = {
            "choices": [
                {"message": {"content": "Hello world"}, "finish_reason": "stop"}
            ],
            "model": "gpt-4",
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = self._make_response(
            status_code=200, json_data=json_data
        )

        with patch.object(
            self.adapter, "_get_client", return_value=mock_client
        ):
            result = await self.adapter.invoke("Say hello")

        assert result.success is True
        assert result.output == "Hello world"
        assert result.error is None
        assert result.metadata["model"] == "gpt-4"
        assert result.metadata["finish_reason"] == "stop"
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "/v1/chat/completions"
        payload = call_args[1]["json"]
        assert payload["model"] == "gpt-4"
        assert payload["messages"][1]["content"] == "Say hello"

    @pytest.mark.asyncio
    async def test_openai_adapter_invoke_http_error(self):
        """An HTTP 500 response yields an error AgentResult."""
        resp = self._make_response(status_code=500, json_data={"error": "boom"})
        resp.text = '{"error": "boom"}'
        mock_client = AsyncMock()
        mock_client.post.return_value = resp

        with patch.object(
            self.adapter, "_get_client", return_value=mock_client
        ):
            result = await self.adapter.invoke("fail")

        assert result.success is False
        assert "HTTP 500" in result.error
        assert result.metadata["status_code"] == 500

    @pytest.mark.asyncio
    async def test_openai_adapter_invoke_request_error(self):
        """A connection error (httpx.RequestError) yields an error AgentResult."""
        import httpx

        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.ConnectError("connection refused")

        with patch.object(
            self.adapter, "_get_client", return_value=mock_client
        ):
            result = await self.adapter.invoke("fail")

        assert result.success is False
        assert "Request error" in result.error

    @pytest.mark.asyncio
    async def test_openai_adapter_health_check_ok(self):
        """GET /v1/models returning 200 means healthy."""
        mock_client = AsyncMock()
        mock_client.get.return_value = self._make_response(status_code=200)

        with patch.object(
            self.adapter, "_get_client", return_value=mock_client
        ):
            healthy = await self.adapter.health_check()

        assert healthy is True
        mock_client.get.assert_called_once_with("/v1/models")

    @pytest.mark.asyncio
    async def test_openai_adapter_health_check_fallback(self):
        """If /v1/models raises an exception but /health returns 200, still healthy."""
        import httpx

        mock_client = AsyncMock()
        # First call to /v1/models raises, second call to /health succeeds
        mock_resp_200 = self._make_response(status_code=200)
        mock_client.get.side_effect = [
            httpx.ConnectError("connection refused"),
            mock_resp_200,
        ]

        with patch.object(
            self.adapter, "_get_client", return_value=mock_client
        ):
            healthy = await self.adapter.health_check()

        assert healthy is True
        assert mock_client.get.call_count == 2

    @pytest.mark.asyncio
    async def test_openai_adapter_health_check_fail(self):
        """Both endpoints failing means unhealthy."""
        mock_client = AsyncMock()
        mock_client.get.side_effect = Exception("down")

        with patch.object(
            self.adapter, "_get_client", return_value=mock_client
        ):
            healthy = await self.adapter.health_check()

        assert healthy is False

    def test_openai_adapter_name(self):
        assert self.adapter.name == "openai:gpt-4@http://localhost:8000"

    def test_openai_adapter_capabilities(self):
        caps = self.adapter.capabilities
        assert isinstance(caps, AgentCapabilities)
        assert "chat" in caps.skills
        assert "text" in caps.input_modes


# ====================================================================
# CLI Adapter
# ====================================================================


class TestCLIAdapter:
    """Tests for CLIAdapter.invoke, .health_check, and .name."""

    def setup_method(self):
        self.adapter = CLIAdapter(
            command="echo",
            args=["-n"],
            timeout=10,
            workdir="/tmp",
        )

    @pytest.mark.asyncio
    async def test_cli_adapter_invoke_success(self):
        """Successful subprocess produces a success result."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"hello", b"")
        mock_proc.returncode = 0

        with patch("src.adapters.cli_adapter.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await self.adapter.invoke("hello")

        assert result.success is True
        assert result.output == "hello"

    @pytest.mark.asyncio
    async def test_cli_adapter_invoke_json_output(self):
        """If stdout is JSON with 'output' key, it's extracted."""
        payload = json.dumps({"output": "parsed result", "artifacts": ["a.txt"]})
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (payload.encode(), b"")
        mock_proc.returncode = 0

        with patch("src.adapters.cli_adapter.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await self.adapter.invoke("task")

        assert result.success is True
        assert result.output == "parsed result"
        assert result.artifacts == ["a.txt"]
        assert result.metadata["parsed_json"] is True

    @pytest.mark.asyncio
    async def test_cli_adapter_invoke_nonzero_exit(self):
        """Non-zero exit code yields error AgentResult."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"something went wrong")
        mock_proc.returncode = 1

        with patch("src.adapters.cli_adapter.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await self.adapter.invoke("task")

        assert result.success is False
        assert "Exit code 1" in result.error
        assert "something went wrong" in result.error

    @pytest.mark.asyncio
    async def test_cli_adapter_invoke_timeout(self):
        """Timeout triggers kill and returns error AgentResult."""
        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = asyncio.TimeoutError()
        mock_proc.returncode = -9  # SIGKILL

        with patch("src.adapters.cli_adapter.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await self.adapter.invoke("slow task")

        assert result.success is False
        assert "timed out" in result.error
        assert result.metadata["timeout"] is True
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cli_adapter_invoke_file_not_found(self):
        """If the command doesn't exist, FileNotFoundError returns error."""
        with patch(
            "src.adapters.cli_adapter.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("No such file"),
        ):
            result = await self.adapter.invoke("task")

        assert result.success is False
        assert "Command not found" in result.error

    @pytest.mark.asyncio
    async def test_cli_adapter_health_check_exists(self):
        """shutil.which returning a path means healthy."""
        with patch("src.adapters.cli_adapter.shutil.which", return_value="/usr/bin/echo"):
            assert await self.adapter.health_check() is True

    @pytest.mark.asyncio
    async def test_cli_adapter_health_check_missing(self):
        """shutil.which returning None means unhealthy."""
        with patch("src.adapters.cli_adapter.shutil.which", return_value=None):
            assert await self.adapter.health_check() is False

    def test_cli_adapter_name(self):
        assert self.adapter.name == "cli:echo"

    def test_cli_adapter_capabilities(self):
        caps = self.adapter.capabilities
        assert "execute" in caps.skills
        assert "text" in caps.output_modes


# ====================================================================
# MCP Adapter
# ====================================================================


class TestMCPAdapter:
    """Tests for MCPAdapter.invoke, .health_check, and .name."""

    def setup_method(self):
        self.adapter = MCPAdapter(
            server_url="http://localhost:9000",
            timeout=15,
        )

    @pytest.mark.asyncio
    async def test_mcp_adapter_invoke_success(self):
        """Normal tools/call response with text content."""
        json_data = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {"type": "text", "text": "MCP result line 1"},
                    {"type": "text", "text": "MCP result line 2"},
                ],
                "isError": False,
            },
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = self._make_mcp_response(200, json_data)

        with patch.object(
            self.adapter, "_get_client", return_value=mock_client
        ):
            result = await self.adapter.invoke("do something", {"tool_name": "my_tool"})

        assert result.success is True
        assert "MCP result line 1" in result.output
        assert "MCP result line 2" in result.output

    @pytest.mark.asyncio
    async def test_mcp_adapter_invoke_error_field(self):
        """JSON-RPC response with 'error' key returns failure."""
        json_data = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = self._make_mcp_response(200, json_data)

        with patch.object(
            self.adapter, "_get_client", return_value=mock_client
        ):
            result = await self.adapter.invoke("task")

        assert result.success is False
        assert "JSON-RPC error -32600" in result.error

    @pytest.mark.asyncio
    async def test_mcp_adapter_invoke_is_error_true(self):
        """MCP result with isError=True returns failure."""
        json_data = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "tool failed"}],
                "isError": True,
            },
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = self._make_mcp_response(200, json_data)

        with patch.object(
            self.adapter, "_get_client", return_value=mock_client
        ):
            result = await self.adapter.invoke("task")

        assert result.success is False
        assert "tool failed" in result.error

    @pytest.mark.asyncio
    async def test_mcp_adapter_invoke_http_error(self):
        """HTTP 502 yields error AgentResult."""
        import httpx

        resp = MagicMock()
        resp.status_code = 502
        resp.text = "bad gateway"
        error = httpx.HTTPStatusError("502", request=MagicMock(), response=resp)
        resp.raise_for_status.side_effect = error
        mock_client = AsyncMock()
        mock_client.post.return_value = resp

        with patch.object(
            self.adapter, "_get_client", return_value=mock_client
        ):
            result = await self.adapter.invoke("task")

        assert result.success is False
        assert "HTTP 502" in result.error

    @pytest.mark.asyncio
    async def test_mcp_adapter_invoke_with_resource_artifacts(self):
        """MCP response with resource-type items produces artifacts."""
        json_data = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {"type": "text", "text": "done"},
                    {"type": "resource", "resource": "file:///tmp/out.txt"},
                ],
                "isError": False,
            },
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = self._make_mcp_response(200, json_data)

        with patch.object(
            self.adapter, "_get_client", return_value=mock_client
        ):
            result = await self.adapter.invoke("task")

        assert result.success is True
        assert len(result.artifacts) == 1

    @pytest.mark.asyncio
    async def test_mcp_adapter_health_check_ok(self):
        """tools/list returns 200 with valid JSON-RPC -> healthy."""
        json_data = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": [{"name": "foo"}]},
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = self._make_mcp_response(200, json_data)

        with patch.object(
            self.adapter, "_get_client", return_value=mock_client
        ):
            healthy = await self.adapter.health_check()

        assert healthy is True
        # Verify the JSON-RPC payload sent
        call_args = mock_client.post.call_args
        sent = call_args[1]["json"]
        assert sent["method"] == "tools/list"

    @pytest.mark.asyncio
    async def test_mcp_adapter_health_check_error_response(self):
        """tools/list returns JSON-RPC error -> unhealthy."""
        json_data = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "Method not found"},
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = self._make_mcp_response(200, json_data)

        with patch.object(
            self.adapter, "_get_client", return_value=mock_client
        ):
            healthy = await self.adapter.health_check()

        assert healthy is False

    @pytest.mark.asyncio
    async def test_mcp_adapter_health_check_exception(self):
        """Network failure during tools/list -> unhealthy."""
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("connection refused")

        with patch.object(
            self.adapter, "_get_client", return_value=mock_client
        ):
            healthy = await self.adapter.health_check()

        assert healthy is False

    def test_mcp_adapter_name(self):
        assert self.adapter.name == "mcp:http://localhost:9000"

    def test_mcp_adapter_capabilities(self):
        caps = self.adapter.capabilities
        assert "tools" in caps.skills

    # -- helper --

    @staticmethod
    def _make_mcp_response(status_code, json_data):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_data
        resp.raise_for_status.return_value = None
        return resp


# ====================================================================
# AdapterManager
# ====================================================================


class TestAdapterManager:
    """Tests for AdapterManager register/get/unregister and create_adapter factory."""

    def test_adapter_manager_register_get(self):
        """Register a backend and retrieve it by agent_id."""
        mgr = AdapterManager()
        mock_backend = MagicMock(spec=AgentBackend)
        mock_backend.name = "mock:test"

        mgr.register("agent-1", mock_backend)
        assert mgr.get("agent-1") is mock_backend
        assert mgr.get("nonexistent") is None

    def test_adapter_manager_unregister(self):
        """Unregister removes and returns the adapter."""
        mgr = AdapterManager()
        mock_backend = MagicMock(spec=AgentBackend)
        mock_backend.name = "mock:test"

        mgr.register("agent-1", mock_backend)
        removed = mgr.unregister("agent-1")
        assert removed is mock_backend
        assert mgr.get("agent-1") is None

    def test_adapter_manager_list_agents(self):
        """list_agents returns all registered IDs."""
        mgr = AdapterManager()
        mgr.register("a", MagicMock())
        mgr.register("b", MagicMock())
        assert set(mgr.list_agents()) == {"a", "b"}

    def test_adapter_manager_register_from_info(self):
        """register_from_info creates adapter and registers it."""
        mgr = AdapterManager()
        adapter = mgr.register_from_info("agent-openai", {
            "protocol": "openai",
            "base_url": "http://localhost:8000",
            "model": "gpt-4",
        })
        assert isinstance(adapter, OpenAIAdapter)
        assert mgr.get("agent-openai") is adapter

    def test_adapter_manager_create_openai(self):
        """Factory creates OpenAIAdapter for protocol='openai'."""
        adapter = create_adapter({
            "protocol": "openai",
            "base_url": "http://llm:11434",
            "api_key": "sk-test",
            "model": "llama3",
        })
        assert isinstance(adapter, OpenAIAdapter)
        assert adapter.base_url == "http://llm:11434"
        assert adapter.api_key == "sk-test"
        assert adapter.model == "llama3"

    def test_adapter_manager_create_cli(self):
        """Factory creates CLIAdapter for protocol='cli'."""
        adapter = create_adapter({
            "protocol": "cli",
            "command": "python3",
            "args": ["-u", "agent.py"],
            "timeout": 60,
            "workdir": "/app",
        })
        assert isinstance(adapter, CLIAdapter)
        assert adapter.command == "python3"
        assert adapter.args == ["-u", "agent.py"]

    def test_adapter_manager_create_mcp(self):
        """Factory creates MCPAdapter for protocol='mcp'."""
        adapter = create_adapter({
            "protocol": "mcp",
            "server_url": "http://mcp-server:5000",
            "timeout": 20,
        })
        assert isinstance(adapter, MCPAdapter)
        assert adapter.server_url == "http://mcp-server:5000"

    def test_adapter_manager_create_unknown_protocol(self):
        """Factory raises ValueError for unknown protocol."""
        with pytest.raises(ValueError, match="Unknown protocol"):
            create_adapter({"protocol": "websocket"})

    def test_adapter_manager_create_a2a(self):
        """Protocol 'a2a' creates a real A2AAdapter (R1.4)."""
        from src.adapters.a2a_adapter import A2AAdapter
        adapter = create_adapter({
            "protocol": "a2a",
            "base_url": "http://a2a:8080",
        })
        assert isinstance(adapter, A2AAdapter)
        assert adapter.base_url == "http://a2a:8080"

    def test_adapter_manager_overwrite_warns(self):
        """Re-registering an agent overwrites the existing adapter."""
        mgr = AdapterManager()
        first = MagicMock(spec=AgentBackend, name="first")
        second = MagicMock(spec=AgentBackend, name="second")
        mgr.register("a", first)
        mgr.register("a", second)  # should overwrite
        assert mgr.get("a") is second


# ====================================================================
# Circuit Breaker
# ====================================================================


class TestCircuitBreaker:
    """Tests for CircuitBreaker state machine and call wrapper."""

    def test_circuit_breaker_closed(self):
        """A new circuit breaker starts in CLOSED state."""
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_circuit_breaker_closed_allows_calls(self):
        """In CLOSED state, calls pass through and succeed."""
        cb = CircuitBreaker(failure_threshold=3)

        async def ok():
            return "result"

        result = await cb.call(ok)
        assert result == "result"
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_on_failures(self):
        """Circuit opens after N consecutive failures."""
        cb = CircuitBreaker(failure_threshold=3, success_threshold=2)

        async def fail():
            raise RuntimeError("boom")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                await cb.call(fail)

        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_circuit_breaker_rejects_when_open(self):
        """When OPEN, call() raises CircuitOpenError."""
        cb = CircuitBreaker(failure_threshold=1, timeout=60)

        async def fail():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await cb.call(fail)

        assert cb.state == CircuitState.OPEN

        with pytest.raises(CircuitOpenError):
            await cb.call(lambda: asyncio.sleep(0))

    @pytest.mark.asyncio
    async def test_circuit_breaker_half_open_recovers(self):
        """After timeout, circuit transitions HALF_OPEN, then back to CLOSED."""
        cb = CircuitBreaker(
            failure_threshold=2,
            success_threshold=2,
            timeout=0,  # immediate transition
        )

        async def fail():
            raise RuntimeError("boom")

        # Trip the breaker
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(fail)

        # With timeout=0, accessing .state transitions immediately to HALF_OPEN
        assert cb.state == CircuitState.HALF_OPEN

        # Two successes should close the circuit
        async def ok():
            return "ok"

        r1 = await cb.call(ok)
        assert r1 == "ok"
        assert cb.state == CircuitState.HALF_OPEN  # need one more

        r2 = await cb.call(ok)
        assert r2 == "ok"
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_circuit_breaker_half_open_failure_reopens(self):
        """A failure in HALF_OPEN immediately reopens the circuit."""
        cb = CircuitBreaker(
            failure_threshold=2,
            success_threshold=2,
            timeout=0,
        )

        async def fail():
            raise RuntimeError("boom")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(fail)

        # With timeout=0, .state transitions to HALF_OPEN immediately
        assert cb.state == CircuitState.HALF_OPEN

        # One success, then a failure should re-open
        async def ok():
            return "ok"

        await cb.call(ok)
        assert cb.state == CircuitState.HALF_OPEN

        with pytest.raises(RuntimeError):
            await cb.call(fail)

        # After a failure in HALF_OPEN, it goes to OPEN.
        # But with timeout=0, reading .state immediately goes back to HALF_OPEN.
        # Use _state to check the raw internal state before the auto-transition.
        assert cb._state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_circuit_breaker_slow_call_detected(self):
        """A call exceeding slow_call_threshold is recorded as a failure."""
        cb = CircuitBreaker(
            failure_threshold=1,
            slow_call_threshold=0.01,  # 10ms
        )
        call_count = 0

        async def slow_call():
            await asyncio.sleep(0.05)  # 50ms > 10ms threshold
            return "slow"

        result = await cb.call(slow_call)
        # The call itself still returns the result
        assert result == "slow"
        # But it should have been recorded as a failure
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_circuit_breaker_reset(self):
        """reset() forces the circuit back to CLOSED regardless of state."""
        cb = CircuitBreaker(failure_threshold=1)

        async def fail():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await cb.call(fail)

        assert cb.state == CircuitState.OPEN

        cb.reset()
        assert cb.state == CircuitState.CLOSED

        # After reset, calls should work again
        async def ok():
            return "ok"

        result = await cb.call(ok)
        assert result == "ok"
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_circuit_breaker_timeout_elapsed_opens_half_open(self):
        """After the timeout elapses, the state automatically transitions to HALF_OPEN."""
        cb = CircuitBreaker(failure_threshold=2, timeout=1)

        async def fail():
            raise RuntimeError("boom")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(fail)

        assert cb.state == CircuitState.OPEN

        # Manually wind back the clock
        cb._opened_at = time.monotonic() - 2  # 2s ago, timeout=1s
        assert cb.state == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_circuit_breaker_call_passes_args(self):
        """call() correctly forwards *args and **kwargs."""
        cb = CircuitBreaker()

        async def add(a, b, c=0):
            return a + b + c

        result = await cb.call(add, 1, 2, c=3)
        assert result == 6

    def test_circuit_open_error_is_exception(self):
        """CircuitOpenError is a proper Exception subclass."""
        assert issubclass(CircuitOpenError, Exception)

    def test_circuit_state_enum(self):
        """CircuitState has exactly CLOSED, OPEN, HALF_OPEN."""
        assert set(CircuitState) == {
            CircuitState.CLOSED,
            CircuitState.OPEN,
            CircuitState.HALF_OPEN,
        }


# ====================================================================
# AgentResult / AgentCapabilities dataclasses
# ====================================================================


class TestAgentResult:
    """Basic tests for the AgentResult dataclass."""

    def test_success_result(self):
        r = AgentResult(success=True, output="hello")
        assert r.success is True
        assert r.output == "hello"
        assert r.error is None
        assert r.artifacts == []
        assert r.metadata == {}

    def test_error_result(self):
        r = AgentResult(
            success=False, output="", error="something failed", metadata={"code": 500}
        )
        assert r.success is False
        assert r.error == "something failed"
        assert r.metadata["code"] == 500

    def test_result_with_artifacts(self):
        r = AgentResult(
            success=True, output="done", artifacts=["file1.txt", "file2.txt"]
        )
        assert len(r.artifacts) == 2


class TestAgentCapabilities:
    """Basic tests for the AgentCapabilities dataclass."""

    def test_defaults(self):
        caps = AgentCapabilities()
        assert caps.skills == []
        assert caps.input_modes == []
        assert caps.output_modes == []

    def test_with_values(self):
        caps = AgentCapabilities(
            skills=["chat"], input_modes=["text"], output_modes=["text", "json"]
        )
        assert caps.skills == ["chat"]
        assert "json" in caps.output_modes
