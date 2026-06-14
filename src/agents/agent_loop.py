"""Extracted agent LLM tool-calling loop (worker-side, testable).

Pulled out of ``worker.call_llm`` so the loop is unit-testable without Docker or
a real LLM: ``llm_call`` and ``execute_tool`` are injected, and an optional
``on_progress`` callback receives a per-step event (used to surface mid-flight
progress to ``tasks/get`` polling).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


async def run_agent_loop(
    *,
    llm_call: Callable[[list[dict]], Awaitable[Any]],
    execute_tool: Callable[[str, dict], str],
    system_prompt: str,
    user_message: str,
    max_iterations: int = 20,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> str:
    """Run the LLM tool-calling loop and return the final assistant text.

    Parameters
    ----------
    llm_call:
        Async callable mapping the current ``messages`` list to an OpenAI-style
        chat completion response (``response.choices[0].message``).
    execute_tool:
        Sync callable ``(name, args_dict) -> result_str``.
    on_progress:
        Optional callback invoked with a progress event each step.
    """
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    final_response = ""

    for step in range(max_iterations):
        response = await llm_call(messages)
        msg = response.choices[0].message
        messages.append(msg.model_dump())

        if on_progress is not None:
            on_progress({
                "step": step,
                "type": "assistant",
                "content": (msg.content or "")[:500],
            })

        if not msg.tool_calls:
            final_response = msg.content or ""
            break

        for tool_call in msg.tool_calls:
            fn_name = tool_call.function.name
            try:
                fn_args = json.loads(tool_call.function.arguments)
            except Exception:
                logger.warning("Bad tool args for %s: %r", fn_name, tool_call.function.arguments)
                fn_args = {}

            result = execute_tool(fn_name, fn_args)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

            if on_progress is not None:
                on_progress({
                    "step": step,
                    "type": "tool",
                    "tool": fn_name,
                    "result": (result or "")[:200],
                })

    return final_response
