"""R1.3 tests: /invoke is protected by a per-agent circuit breaker.

Repeated adapter failures must trip a per-agent CircuitBreaker; once open, further
invocations return 503 (circuit open) instead of hammering the failing agent.
"""
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from starlette.testclient import TestClient

from src.gateway.routes import router, set_deps

BASE = "/api/v1/agents"


def _client_with_adapter(adapter):
    registry = MagicMock()  # non-None placeholder; /invoke does not call the registry
    adapter_manager = MagicMock()
    adapter_manager.get = MagicMock(return_value=adapter)
    app = FastAPI()
    app.include_router(router)
    set_deps(registry, adapter_manager)
    return TestClient(app, raise_server_exceptions=False)


class TestInvokeCircuitBreaker:
    def test_repeated_failures_open_circuit_then_503(self):
        failing = MagicMock()
        failing.invoke = AsyncMock(side_effect=RuntimeError("boom"))
        client = _client_with_adapter(failing)

        # CircuitBreaker defaults: failure_threshold=5. The first 5 failing calls
        # each propagate the error (500) and accumulate failures; the 5th trips it.
        for i in range(5):
            r = client.post(f"{BASE}/agent-x/invoke", json={"task": "x"})
            assert r.status_code == 500, f"call {i}: {r.text}"

        # 6th call: circuit is OPEN -> 503, adapter is NOT called again
        before = failing.invoke.await_count
        r = client.post(f"{BASE}/agent-x/invoke", json={"task": "x"})
        assert r.status_code == 503
        assert failing.invoke.await_count == before  # short-circuited, no new call

    def test_success_does_not_trip_circuit(self):
        from src.adapters.base import AgentResult
        ok = MagicMock()
        ok.invoke = AsyncMock(return_value=AgentResult(success=True, output="done"))
        client = _client_with_adapter(ok)

        for _ in range(10):
            r = client.post(f"{BASE}/agent-y/invoke", json={"task": "x"})
            assert r.status_code == 200
            assert r.json()["success"] is True
