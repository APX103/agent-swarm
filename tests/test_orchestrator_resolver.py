"""R3.1–R3.3 tests: pluggable orchestrator — protocol, resolver, external orchestrator.

The orchestrator loop becomes a selectable backend: ``builtin`` (the existing
Orchestrator) or ``external`` (an A2A scheduler agent). The resolver picks per
config and falls back to builtin on external failure (with an explicit event).
"""
import pytest
from unittest.mock import MagicMock

from src.orchestrator.base import OrchestratorBackend, OrchestratorConfig
from src.orchestrator.resolver import OrchestratorResolver


class FakeOrchestrator:
    """Minimal orchestrator-shaped object for resolver tests."""

    def __init__(self, result: str = "builtin-ok", raise_exc: Exception | None = None):
        self._result = result
        self._raise = raise_exc
        self.executed = False

    async def execute(self, task_id, tenant_id, user_message, event_callback=None) -> str:
        self.executed = True
        if self._raise:
            raise self._raise
        return self._result


# ── R3.1 protocol ──────────────────────────────────────────────────────────────


def test_builtin_orchestrator_satisfies_protocol():
    from src.orchestrator.orchestrator import Orchestrator

    assert hasattr(Orchestrator, "execute")
    assert isinstance(FakeOrchestrator(), OrchestratorBackend)  # runtime_checkable


# ── R3.2 resolver ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_provider_builtin_uses_builtin():
    builtin = FakeOrchestrator("B")
    resolver = OrchestratorResolver(builtin, OrchestratorConfig(provider="builtin"))
    assert await resolver.execute("t", "ten", "hi") == "B"
    assert builtin.executed


@pytest.mark.asyncio
async def test_provider_external_uses_external(monkeypatch):
    builtin = FakeOrchestrator("B")

    class FakeExternal:
        def __init__(self, endpoint, timeout=600.0):
            pass

        async def execute(self, *a, **k):
            return "E"

    monkeypatch.setattr("src.orchestrator.resolver.ExternalOrchestrator", FakeExternal)
    resolver = OrchestratorResolver(
        builtin, OrchestratorConfig(provider="external", external_endpoint="http://ext")
    )
    assert await resolver.execute("t", "ten", "hi") == "E"
    assert not builtin.executed


@pytest.mark.asyncio
async def test_external_failure_falls_back_to_builtin(monkeypatch):
    builtin = FakeOrchestrator("B")

    class FakeExternal:
        def __init__(self, endpoint, timeout=600.0):
            pass

        async def execute(self, *a, **k):
            raise RuntimeError("ext down")

    monkeypatch.setattr("src.orchestrator.resolver.ExternalOrchestrator", FakeExternal)
    events: list[dict] = []

    async def cb(e):
        events.append(e)

    resolver = OrchestratorResolver(
        builtin,
        OrchestratorConfig(provider="external", external_endpoint="http://ext", fallback=True),
    )
    assert await resolver.execute("t", "ten", "hi", event_callback=cb) == "B"
    assert builtin.executed
    assert any(e.get("type") == "orchestrator_fallback" for e in events)


@pytest.mark.asyncio
async def test_external_failure_no_fallback_raises(monkeypatch):
    builtin = FakeOrchestrator("B")

    class FakeExternal:
        def __init__(self, endpoint, timeout=600.0):
            pass

        async def execute(self, *a, **k):
            raise RuntimeError("ext down")

    monkeypatch.setattr("src.orchestrator.resolver.ExternalOrchestrator", FakeExternal)
    resolver = OrchestratorResolver(
        builtin,
        OrchestratorConfig(provider="external", external_endpoint="http://ext", fallback=False),
    )
    with pytest.raises(RuntimeError):
        await resolver.execute("t", "ten", "hi")


@pytest.mark.asyncio
async def test_external_no_endpoint_falls_back():
    builtin = FakeOrchestrator("B")
    resolver = OrchestratorResolver(
        builtin, OrchestratorConfig(provider="external", external_endpoint="", fallback=True)
    )
    assert await resolver.execute("t", "ten", "hi") == "B"  # no endpoint -> builtin


# ── R3.3 ExternalOrchestrator ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_external_orchestrator_returns_summary(monkeypatch):
    from src.orchestrator.external import ExternalOrchestrator

    fake_task = MagicMock(message="SCHED-OK", state="completed", task_id="t1")

    class FakeClient:
        def __init__(self, base_url, timeout=600.0):
            self.base_url = base_url

        async def send_message(self, msg, blocking=True, configuration=None):
            return fake_task

        async def poll_task(self, task_id, interval=2.0, timeout=600.0):
            yield fake_task

        async def close(self):
            pass

    monkeypatch.setattr("src.orchestrator.external.A2AClient", FakeClient)
    ext = ExternalOrchestrator("http://sched")
    assert await ext.execute("t", "ten", "do it") == "SCHED-OK"


@pytest.mark.asyncio
async def test_external_orchestrator_raises_on_no_task(monkeypatch):
    from src.orchestrator.external import ExternalOrchestrator

    class FakeClient:
        def __init__(self, base_url, timeout=600.0):
            pass

        async def send_message(self, msg, blocking=True, configuration=None):
            return None

        async def close(self):
            pass

    monkeypatch.setattr("src.orchestrator.external.A2AClient", FakeClient)
    ext = ExternalOrchestrator("http://sched")
    with pytest.raises(RuntimeError):
        await ext.execute("t", "ten", "x")


def test_default_config_has_builtin_orchestrator():
    """R3.5: config/default.yaml yields provider=builtin (zero behaviour change)."""
    from src.config import load_settings

    s = load_settings()
    assert s.orchestrator.provider == "builtin"
    assert s.orchestrator.fallback is True
    assert s.orchestrator.external_endpoint == ""
