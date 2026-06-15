"""TRUE end-to-end direct-chat: real Dispatcher (by-id selection) → real
ExternalAgentBackend → real A2AAdapter (streaming) → (mocked A2A worker via respx).

Direct-chat bypasses the orchestrator/LLM entirely: a DispatchRequest with
agent_id set routes straight to one specific agent. This proves the streaming
path (1a) + by-id selection (1b) work through real production code, with only
the A2A HTTP boundary mocked.

Verifies end-to-end:
- by-id candidate resolution picks exactly the target agent (not skill-matched)
- the real A2AAdapter sends non-blocking + polls + forwards on_progress snapshots
- progress snapshots carry state + message + progress (the new field)
- a completed task yields a successful DispatchResult with the agent's reply
"""
import json
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from src.adapters.adapter_manager import AdapterManager
from src.adapters.a2a_adapter import A2AAdapter
from src.dispatcher.backends import ExternalAgentBackend
from src.dispatcher.base import DispatchRequest
from src.dispatcher.dispatcher import Dispatcher, DispatcherConfig
from src.registry.registry import AgentRegistry

from tests.test_registry import FakeRedis


def _a2a_result(task_id, state, agent_text="", progress=None):
    history = []
    if agent_text:
        history.append({"role": "agent", "parts": [{"kind": "text", "text": agent_text}]})
    return {
        "jsonrpc": "2.0", "id": 1,
        "result": {
            "id": task_id, "status": {"state": state},
            "history": history, "progress": progress or [],
        },
    }


@pytest.mark.asyncio
@respx.mock
async def test_direct_chat_full_streaming_path():
    """Direct-chat by agent_id → real adapter streaming → progress forwarded → success."""
    # real registry + FakeRedis
    registry = AgentRegistry(redis_url="redis://x", heartbeat_ttl=30, heartbeat_interval=10)
    registry._redis = FakeRedis({})

    # register TWO agents sharing the same skill, so we can prove by-id selection
    # picks the right one (not skill-matched to whichever comes first)
    target_id = await registry.register({
        "name": "Target Agent", "endpoint": "http://target:9001",
        "protocol": "a2a", "skills": ["frontend-engineer"],
    })
    decoy_id = await registry.register({
        "name": "Decoy Agent", "endpoint": "http://decoy:9001",
        "protocol": "a2a", "skills": ["frontend-engineer"],
    })

    # real adapters for both; only the target's HTTP is wired through respx
    adapter_mgr = AdapterManager()
    adapter_mgr.register(target_id, A2AAdapter(base_url="http://target:9001", timeout=10))
    adapter_mgr.register(decoy_id, A2AAdapter(base_url="http://decoy:9001", timeout=10))

    # mock the TARGET A2A worker: non-blocking send → working; poll → progress then done
    target_calls = {"send": False, "n": 0}
    send_resp = _a2a_result("dc-1", "working")
    poll_progress = _a2a_result("dc-1", "working", progress=[{"step": 0, "type": "tool", "tool": "write_file"}])
    poll_done = _a2a_result("dc-1", "completed", agent_text="已生成 index.html")

    def target_handler(request):
        body = json.loads(request.content)
        method = body.get("method")
        if method == "message/send":
            target_calls["send"] = True
            payload = send_resp
        elif method == "tasks/get":
            target_calls["n"] += 1
            payload = poll_progress if target_calls["n"] == 1 else poll_done
        else:
            payload = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "nope"}}
        return httpx.Response(200, json=payload)

    respx.post("http://target:9001").mock(side_effect=target_handler)

    # real Dispatcher with ExternalAgentBackend
    dispatcher = Dispatcher(
        [ExternalAgentBackend(registry=registry, adapter_manager=adapter_mgr)],
        DispatcherConfig(max_retries=1, health_precheck=False),
    )

    # collect progress snapshots forwarded by the real adapter
    progress_snaps = []

    async def on_progress(snap):
        progress_snaps.append(snap)

    # dispatch DIRECTLY to the target by id (direct-chat contract)
    request = DispatchRequest(
        agent_type="frontend-engineer",
        task="写个登录页",
        context={"task_id": "dc-task", "tenant_id": "default"},
        on_progress=on_progress,
        agent_id=target_id,
    )
    result = await dispatcher.dispatch(request)

    # ── assertions over the real chain ──────────────────────────────────────

    # by-id selection routed to the target, never touched the decoy
    assert result.success is True
    assert result.target.agent_id == target_id
    assert result.output == "已生成 index.html"

    # the target A2A worker was really hit (real adapter → real httpx → respx)
    assert target_calls["send"] is True
    assert target_calls["n"] >= 2  # at least one progress poll + terminal poll

    # streaming: on_progress received snapshots with state + message + progress
    assert len(progress_snaps) >= 1
    # the last snapshot reflects completion
    assert progress_snaps[-1]["state"] == "completed"
    # progress payloads carried through (the new A2ATask.progress field)
    has_progress_payload = any(
        isinstance(s.get("progress"), list) and len(s["progress"]) > 0
        for s in progress_snaps
    )
    assert has_progress_payload, "progress snapshots never carried worker progress entries"


@pytest.mark.asyncio
@respx.mock
async def test_direct_chat_fails_over_by_id_when_target_down():
    """If the by-id target's first attempt fails, the dispatcher has no other
    candidate for that id (by-id returns exactly one), so the result is failure
    — distinct from skill-based dispatch which would try siblings."""
    registry = AgentRegistry(redis_url="redis://x", heartbeat_ttl=30, heartbeat_interval=10)
    registry._redis = FakeRedis({})
    target_id = await registry.register({
        "name": "Down Agent", "endpoint": "http://down:9001",
        "protocol": "a2a", "skills": ["x"],
    })

    adapter_mgr = AdapterManager()
    adapter_mgr.register(target_id, A2AAdapter(base_url="http://down:9001", timeout=5))

    # worker returns an error / no task
    respx.post("http://down:9001").mock(
        side_effect=lambda req: httpx.Response(200, json={
            "jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "boom"},
        })
    )

    dispatcher = Dispatcher(
        [ExternalAgentBackend(registry=registry, adapter_manager=adapter_mgr)],
        DispatcherConfig(max_retries=0, health_precheck=False),
    )

    result = await dispatcher.dispatch(
        DispatchRequest(agent_type="x", task="hi", agent_id=target_id)
    )
    assert result.success is False
    # exactly one attempt (by-id yields a single candidate)
    assert len(result.attempts) == 1
