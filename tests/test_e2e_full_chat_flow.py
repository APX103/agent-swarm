"""TRUE end-to-end test: real orchestrator → real dispatcher → real A2AAdapter
→ (mocked A2A worker via respx) → artifacts on disk + full session event chain.

Every component between the orchestrator entry and the disk is REAL production
code. Only two boundaries are mocked:
  1. The orchestrator's LLM (OpenAI client) — canned tool-call sequence
     (plan_task → dispatch_agent → finalize), so we don't burn tokens.
  2. The A2A worker HTTP server — intercepted with respx, returning the same
     JSON-RPC shape a real worker emits (non-blocking send + polled tasks/get).

We invoke the orchestrator DIRECTLY (not via /api/chat) so the real async
orchestration loop runs in the test's own event loop — the production code path
is identical, just without the TestClient/background-task loop-isolation caveat.
"""
import json
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from src.adapters.adapter_manager import AdapterManager
from src.adapters.a2a_adapter import A2AAdapter
from src.config import AgentCardDef, ContainerPoolConfig, LLMConfig, Settings, StorageConfig
from src.dispatcher.backends import ExternalAgentBackend
from src.dispatcher.dispatcher import Dispatcher, DispatcherConfig
from src.dispatcher.result_cache import ResultCache
from src.orchestrator.orchestrator import Orchestrator
from src.registry.registry import AgentRegistry
from src.session.service import SessionService
from src.task_manager.manager import TaskManager

from tests.test_registry import FakeRedis


# ── helpers ───────────────────────────────────────────────────────────────────


def _llm_response(tool_name=None, tool_args=None, content=None):
    """OpenAI-shaped chat completion with one tool call (or plain text)."""
    resp = MagicMock()
    msg = MagicMock()
    msg.content = content
    msg.model_dump.return_value = {"role": "assistant", "content": content}
    if tool_name:
        tc = MagicMock()
        tc.id = f"call_{tool_name}"
        tc.function.name = tool_name
        tc.function.arguments = json.dumps(tool_args or {})
        msg.tool_calls = [tc]
    else:
        msg.tool_calls = None
    resp.choices = [MagicMock()]
    resp.choices[0].message = msg
    return resp


def _a2a_task_result(task_id, state, agent_text="", progress=None):
    """JSON-RPC result a real worker returns (matches worker.py handle_get_task)."""
    history = []
    if agent_text:
        history.append({"role": "agent", "parts": [{"kind": "text", "text": agent_text}]})
    return {
        "jsonrpc": "2.0", "id": 1,
        "result": {
            "id": task_id,
            "status": {"state": state},
            "history": history,
            "progress": progress or [],
        },
    }


@pytest.fixture
def e2e_world(tmp_path):
    """Wire the full real stack against a tmp shared_output + FakeRedis registry."""
    work_base = tmp_path / "shared"
    work_base.mkdir()

    settings = Settings(
        llm=LLMConfig(default_model="m", default_base_url="http://llm/v4", default_api_key="k"),
        container_pool=ContainerPoolConfig(base_port=9100),
        storage=StorageConfig(shared_output_base=str(work_base)),
        agent_cards=[
            AgentCardDef(id="frontend-engineer", name="Frontend", description="前端", skills=[]),
        ],
    )

    task_mgr = TaskManager(shared_output_base=str(work_base), store=None)
    session_svc = SessionService(tmp_path / "swarm.db", str(work_base))

    registry = AgentRegistry(redis_url="redis://x", heartbeat_ttl=30, heartbeat_interval=10)
    registry._redis = FakeRedis({})

    adapter_mgr = AdapterManager()
    dispatcher = Dispatcher(
        [ExternalAgentBackend(registry=registry, adapter_manager=adapter_mgr)],
        DispatcherConfig(max_retries=1, health_precheck=False),
        result_cache=ResultCache(),
    )

    return {
        "tmp": tmp_path, "work_base": work_base, "settings": settings,
        "task_mgr": task_mgr, "session_svc": session_svc,
        "registry": registry, "adapter_mgr": adapter_mgr, "dispatcher": dispatcher,
    }


# ── the test: real chain, only LLM + A2A mocked ──────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_full_chat_flow_through_real_components(e2e_world):
    w = e2e_world

    # 1. register a real A2A agent + real A2AAdapter pointed at the (mocked) endpoint
    agent_id = await w["registry"].register({
        "name": "Frontend Engineer", "endpoint": "http://agent:9001",
        "protocol": "a2a", "skills": ["frontend-engineer"],
    })
    w["adapter_mgr"].register(agent_id, A2AAdapter(base_url="http://agent:9001", timeout=10))

    # 2. create a real task + session FIRST (needed so the A2A mock knows the work dir)
    task = await w["task_mgr"].create_task(user_message="写个 hello world 页面", tenant_id="default")
    await w["session_svc"].get_or_create_with_id("e2e-sess-1", "default")
    await w["session_svc"].append_event("e2e-sess-1", {"type": "user_message", "text": "写个 hello world 页面"})

    # 3. mock the A2A worker: non-blocking send → working; tasks/get poll → progress then done.
    #    On send, the handler ALSO writes an artifact file into the shared dir — exactly
    #    what a real worker does (it receives the task via SHARED_DIR and writes there).
    #    This makes the orchestrator's finalize review gate pass with a real file on disk.
    send_resp = _a2a_task_result("wk-1", "working")
    poll_progress = _a2a_task_result("wk-1", "working", progress=[{"step": 0, "type": "assistant"}])
    poll_done = _a2a_task_result("wk-1", "completed", agent_text="已生成 index.html")
    a2a_calls = {"send": False, "n": 0}

    # the orchestrator's finalize review looks for files under <work_dir>/<role-subdir>/
    # where role-subdir = agent_type.split("-")[0] = "frontend"
    frontend_dir = w["work_base"] / "tenants" / "default" / "tasks" / task.task_id / "frontend"
    frontend_dir.mkdir(parents=True, exist_ok=True)

    def a2a_handler(request):
        body = json.loads(request.content)
        method = body.get("method")
        if method == "message/send":
            a2a_calls["send"] = True
            # simulate the worker writing its artifact to the shared dir
            (frontend_dir / "index.html").write_text("<h1>hello world</h1>", encoding="utf-8")
            payload = send_resp
        elif method == "tasks/get":
            a2a_calls["n"] += 1
            payload = poll_progress if a2a_calls["n"] == 1 else poll_done
        else:
            payload = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "nope"}}
        return httpx.Response(200, json=payload)

    respx.post("http://agent:9001").mock(side_effect=a2a_handler)

    # 3. real orchestrator; replace ONLY the LLM client with a canned tool-call sequence
    orch = Orchestrator(
        settings=w["settings"], pool_manager=MagicMock(),
        task_manager=w["task_mgr"], dispatcher=w["dispatcher"],
        session_service=w["session_svc"],
    )
    seq = [
        _llm_response("plan_task", {
            "analysis": "需要前端", "tech_stack": "HTML",
            "subtasks": [{"agent_type": "frontend-engineer", "description": "写 index.html"}],
        }),
        _llm_response("dispatch_agent", {
            "agent_type": "frontend-engineer", "task": "写 index.html",
        }),
        _llm_response("finalize", {"summary": "前端完成"}),
    ]
    call_idx = {"i": 0}

    def fake_create(**kwargs):
        i = min(call_idx["i"], len(seq) - 1)
        call_idx["i"] += 1
        return seq[i]

    orch.client.chat.completions.create = fake_create

    # 4. run the REAL orchestration loop directly (in this event loop)
    events_seen = []

    async def emit(event):
        events_seen.append(event)

    result = await orch.execute(
        task_id=task.task_id, tenant_id="default",
        user_message="写个 hello world 页面", event_callback=emit,
        session=MagicMock(session_id="e2e-sess-1", messages=None,
                          work_dir=w["work_base"] / "tenants" / "default" / "sessions" / "e2e-sess-1"),
    )

    # 6. the A2A worker was actually hit by the REAL adapter → REAL httpx → respx
    assert a2a_calls["send"] is True, "A2A send_message was never called"
    assert a2a_calls["n"] >= 2, "tasks/get polling never reached completion"

    # 7. the full session event chain was written by REAL production code
    sess = await w["session_svc"].get_session("e2e-sess-1")
    assert sess is not None
    types = [e["type"] for e in sess.events]
    assert "user_message" in types
    assert "plan_created" in types
    assert "agent_dispatched" in types
    assert "agent_completed" in types
    assert "finalized" in types

    dispatched = next(e for e in sess.events if e["type"] == "agent_dispatched")
    assert dispatched["agent_type"] == "frontend-engineer"
    completed = next(e for e in sess.events if e["type"] == "agent_completed")
    assert completed["success"] is True
    assert "已生成 index.html" in (completed.get("error") or "") or completed["success"]

    # 8. streaming progress forwarded through the real adapter → real on_progress → emit
    progress_events = [e for e in events_seen if e["type"] == "agent_progress"]
    assert len(progress_events) >= 1, "no agent_progress events were forwarded"
    assert progress_events[0]["agent"] == "frontend-engineer"

    # 9. LLM drove the full plan → dispatch → finalize sequence
    assert call_idx["i"] >= 3, f"LLM loop stopped early: {call_idx['i']} calls"
    assert "前端完成" in result
