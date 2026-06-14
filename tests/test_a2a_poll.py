"""W12 tests: A2AClient.poll_task — async iterator over tasks/get snapshots.

Yields when the task snapshot changes (state or message), stops at a terminal
state (completed/failed/canceled) or on timeout.
"""
import httpx
import pytest
import respx

from src.common.a2a_client import A2AClient

BASE = "http://worker.test"


def _rpc(state: str, msg: str = "") -> dict:
    return {
        "jsonrpc": "2.0", "id": 2, "method": "tasks/get",
        "result": {
            "id": "t1",
            "status": {"state": state},
            "history": [{"role": "agent", "parts": [{"kind": "text", "text": msg}], "messageId": "m"}],
            "artifacts": [],
        },
    }


@pytest.mark.asyncio
async def test_poll_task_yields_changes_and_stops_at_terminal(respx_mock):
    # working("") -> working("") [dup, skipped] -> working("step1") -> completed("done")
    responses = [_rpc("working", ""), _rpc("working", ""), _rpc("working", "step1"), _rpc("completed", "done")]
    counter = {"i": 0}

    def handler(_req):
        r = responses[min(counter["i"], len(responses) - 1)]
        counter["i"] += 1
        return httpx.Response(200, json=r)

    respx.post(url__startswith=BASE).mock(side_effect=handler)

    client = A2AClient(BASE, timeout=5)
    try:
        events = [(t.state, t.message) async for t in client.poll_task("t1", interval=0.001, timeout=5)]
    finally:
        await client.close()

    assert events == [("working", ""), ("working", "step1"), ("completed", "done")]


@pytest.mark.asyncio
async def test_poll_task_times_out_without_terminal_state(respx_mock):
    respx.post(url__startswith=BASE).mock(return_value=httpx.Response(200, json=_rpc("working", "")))
    client = A2AClient(BASE, timeout=5)
    try:
        events = [t.state async for t in client.poll_task("t1", interval=0.001, timeout=0.05)]
    finally:
        await client.close()
    assert events == ["working"]  # yielded once, then the timeout elapsed
