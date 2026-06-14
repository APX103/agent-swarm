"""W13 tests: the extracted agent loop emits step progress and returns the final answer.

The loop is extracted from worker.call_llm so it is unit-testable (no Docker, no
real LLM): llm_call and execute_tool are injected, and an on_progress callback
receives a per-step event.
"""
import json

import pytest

from src.agents.agent_loop import run_agent_loop


class _Function:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, name, arguments):
        self.id = "call-1"
        self.function = _Function(name, arguments)


class _Message:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self):
        return {"role": "assistant", "content": self.content}


class _Response:
    def __init__(self, message):
        self.choices = [type("Choice", (), {"message": message})()]


@pytest.mark.asyncio
async def test_loop_returns_final_text_after_tool_call():
    responses = [
        _Response(_Message(content=None, tool_calls=[_ToolCall("write_file", json.dumps({"path": "a", "content": "b"}))])),
        _Response(_Message(content="DONE", tool_calls=None)),
    ]
    seq = {"i": 0}

    async def llm_call(messages):
        r = responses[min(seq["i"], len(responses) - 1)]
        seq["i"] += 1
        return r

    def execute_tool(name, args):
        return f"wrote {args['path']}"

    result = await run_agent_loop(
        llm_call=llm_call, execute_tool=execute_tool,
        system_prompt="s", user_message="u", max_iterations=5,
    )
    assert result == "DONE"


@pytest.mark.asyncio
async def test_loop_emits_progress_events():
    responses = [
        _Response(_Message(content=None, tool_calls=[_ToolCall("write_file", json.dumps({"path": "a", "content": "b"}))])),
        _Response(_Message(content="DONE", tool_calls=None)),
    ]
    seq = {"i": 0}

    async def llm_call(messages):
        r = responses[min(seq["i"], len(responses) - 1)]
        seq["i"] += 1
        return r

    progress = []

    def on_progress(event):
        progress.append(event)

    await run_agent_loop(
        llm_call=llm_call, execute_tool=lambda n, a: "ok",
        system_prompt="s", user_message="u", max_iterations=5, on_progress=on_progress,
    )

    types = [e["type"] for e in progress]
    assert "tool" in types  # tool execution surfaced as progress
    assert any(e["type"] == "assistant" for e in progress)


@pytest.mark.asyncio
async def test_loop_stops_at_max_iterations_without_final():
    async def llm_call(messages):
        # always asks for another tool call, never finishes
        return _Response(_Message(content=None, tool_calls=[_ToolCall("loop", "{}")]))

    result = await run_agent_loop(
        llm_call=llm_call, execute_tool=lambda n, a: "x",
        system_prompt="s", user_message="u", max_iterations=2,
    )
    assert result == ""  # no final text produced within the cap
