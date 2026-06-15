"""Streaming support for A2A — the missing 4-layer gap.

Covers:
- A2ATask carries a `progress` list (parsed from worker's task["progress"]).
- get_task() parses progress from the JSON-RPC result.
- poll_task() yields a new snapshot when progress grows (not only on state/msg change).
- A2AAdapter.invoke(on_progress=...) sends non-blocking + forwards snapshots.
- A2AAdapter.invoke() without on_progress stays blocking (regression).
- ExternalAgentBackend forwards request.on_progress into adapter.invoke (contract check).
"""
import json
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.common.a2a_client import A2AClient, A2AMessage, A2ATask


# ── helpers: build JSON-RPC responses the way the worker emits them ────────────


def _rpc(result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def _task_result(task_id: str, state: str, msg: str = "", progress: Optional[list] = None):
    """A2A tasks/get result shape (mirrors src/agents/worker.py handle_get_task)."""
    history = []
    if msg:
        history.append({"role": "agent", "parts": [{"kind": "text", "text": msg}]})
    return {
        "id": task_id,
        "status": {"state": state},
        "history": history,
        "progress": progress or [],
    }


def _mock_response(payload: dict):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = payload
    return r


@pytest.fixture
def patched_client(monkeypatch):
    """An A2AClient whose underlying httpx client is a controllable AsyncMock.

    We construct the real A2AClient first, then swap its ._client so we don't
    fight import-time patching ordering.
    """
    from src.common.a2a_client import A2AClient

    client = A2AClient("http://agent-x:9001", timeout=5.0)
    mock_http = AsyncMock()
    client._client = mock_http
    yield client, mock_http


# ── A2ATask.progress field + get_task parsing ─────────────────────────────────


def test_a2a_task_has_progress_field():
    t = A2ATask(task_id="t1", state="working")
    assert t.progress == []  # default empty list, never shared


@pytest.mark.asyncio
async def test_get_task_parses_progress(patched_client):
    client, mock_http = patched_client
    mock_http.post = AsyncMock(
        return_value=_mock_response(
            _rpc(_task_result("t1", "working", "thinking", [{"step": 0, "type": "assistant"}]))
        )
    )
    task = await client.get_task("t1")
    assert task is not None
    assert task.progress == [{"step": 0, "type": "assistant"}]


@pytest.mark.asyncio
async def test_send_message_nonblocking_parses_progress(patched_client):
    client, mock_http = patched_client
    mock_http.post = AsyncMock(
        return_value=_mock_response(
            _rpc(_task_result("t1", "working", "", [{"step": 0}]))
        )
    )
    task = await client.send_message(A2AMessage(role="user", text="hi"), blocking=False)
    assert task is not None
    assert task.state == "working"
    assert task.progress == [{"step": 0}]


# ── poll_task yields on progress growth ───────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_task_yields_on_progress_growth(patched_client):
    """poll_task must yield a fresh snapshot when progress length changes,
    not only when (state, message) changes."""
    client, mock_http = patched_client
    seq = [
        _rpc(_task_result("t1", "working", "", [{"step": 0, "type": "assistant"}])),
        _rpc(_task_result("t1", "working", "", [{"step": 0}, {"step": 1, "type": "tool"}])),
        _rpc(
            _task_result(
                "t1", "completed", "done", [{"step": 0}, {"step": 1}, {"step": 2}]
            )
        ),
    ]
    mock_http.post = AsyncMock(side_effect=[_mock_response(p) for p in seq])

    snapshots = []
    async for snap in client.poll_task("t1", interval=0.0, timeout=5.0):
        snapshots.append(snap)

    # three snapshots: each has a distinct progress length
    assert [len(s.progress) for s in snapshots] == [1, 2, 3]
    assert snapshots[-1].state == "completed"


# ── A2AAdapter.invoke streaming path ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_invoke_streaming_forwards_snapshots():
    from src.adapters.a2a_adapter import A2AAdapter

    adapter = A2AAdapter(base_url="http://agent-x:9001", timeout=5)

    # Fake the inner A2AClient: non-blocking send returns working task, then
    # poll_task yields two snapshots (progress grows, then completed).
    fake_client = MagicMock()
    fake_client.send_message = AsyncMock(
        return_value=A2ATask(task_id="t1", state="working")
    )

    async def fake_poll(task_id, interval=2.0, timeout=300.0):
        yield A2ATask(task_id="t1", state="working", message="", progress=[{"step": 0}])
        yield A2ATask(
            task_id="t1", state="completed", message="done", progress=[{"step": 0}, {"step": 1}]
        )

    fake_client.poll_task = fake_poll
    adapter._client = fake_client

    received = []

    async def on_progress(event):
        received.append(event)

    result = await adapter.invoke("build a button", on_progress=on_progress)

    assert result.success is True
    assert result.output == "done"
    # on_progress forwarded both snapshots with progress carried through
    assert [len(e["progress"]) for e in received] == [1, 2]
    # streaming path used non-blocking send
    sent_args = fake_client.send_message.call_args
    assert sent_args.kwargs.get("blocking") is False


@pytest.mark.asyncio
async def test_adapter_invoke_blocking_when_no_progress_callback():
    """Regression: without on_progress the adapter must stay blocking (old behavior)."""
    from src.adapters.a2a_adapter import A2AAdapter

    adapter = A2AAdapter(base_url="http://agent-x:9001", timeout=5)
    fake_client = MagicMock()
    fake_client.send_message = AsyncMock(
        return_value=A2ATask(task_id="t1", state="completed", message="ok")
    )
    adapter._client = fake_client

    result = await adapter.invoke("hi")  # no on_progress

    assert result.success is True
    sent_args = fake_client.send_message.call_args
    assert sent_args.kwargs.get("blocking") is True


# ── ExternalAgentBackend forwards on_progress (contract check) ─────────────────


@pytest.mark.asyncio
async def test_external_backend_forwards_on_progress_to_adapter():
    """ExternalAgentBackend.invoke must pass request.on_progress into adapter.invoke."""
    from src.dispatcher.backends import ExternalAgentBackend
    from src.dispatcher.base import DispatchRequest, DispatchTarget, TargetKind

    registry = MagicMock()
    adapter_manager = MagicMock()
    cb = AsyncMock()
    adapter = MagicMock()
    adapter.invoke = AsyncMock(return_value=MagicMock(success=True, output="ok", error=None))
    adapter_manager.get = MagicMock(return_value=adapter)

    backend = ExternalAgentBackend(registry=registry, adapter_manager=adapter_manager)
    target = DispatchTarget(kind=TargetKind.EXTERNAL, agent_type="fe", agent_id="a1")
    request = DispatchRequest(agent_type="fe", task="x", on_progress=cb)

    await backend.invoke(target, request)

    # on_progress must reach the adapter (passed as 3rd positional arg)
    invoke_args = adapter.invoke.call_args
    assert invoke_args.args[2] is cb
