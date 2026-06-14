"""Wave 4 tests: per-task trace id (contextvar) + logging filter.

A trace id set at the request boundary propagates into every log record for that
task (orchestrator -> dispatcher -> backend), so a task's full path is greppable.
"""
import asyncio
import logging
from contextvars import copy_context

import pytest

from src.observability.trace import TraceIdFilter, get_trace_id, set_trace_id


@pytest.fixture(autouse=True)
def _reset_trace_id():
    set_trace_id(None)
    yield
    set_trace_id(None)


def test_trace_id_set_and_get():
    set_trace_id("t-123")
    assert get_trace_id() == "t-123"


def test_trace_id_default_is_none_in_fresh_context():
    assert copy_context().run(get_trace_id) is None


def test_filter_injects_trace_id():
    set_trace_id("t-9")
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "msg", None, None)
    assert TraceIdFilter().filter(rec) is True
    assert rec.trace_id == "t-9"


def test_filter_dash_when_unset():
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "msg", None, None)
    TraceIdFilter().filter(rec)
    assert rec.trace_id == "-"


@pytest.mark.asyncio
async def test_trace_id_isolated_per_async_task():
    # asyncio tasks copy the context, so a child's set_trace_id must not leak out.
    set_trace_id("parent")
    seen: dict = {}

    async def child():
        seen["before"] = get_trace_id()  # inherits parent's value (context copy)
        set_trace_id("child")
        seen["after"] = get_trace_id()

    await asyncio.gather(child())  # gather wraps the coroutine in a Task (context copy)

    assert seen["before"] == "parent"
    assert seen["after"] == "child"
    assert get_trace_id() == "parent"  # child's set did not leak


def test_filter_attachable_to_handler_emits_formatted_trace_id():
    set_trace_id("t-fmt")
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[trace=%(trace_id)s] %(message)s"))
    handler.addFilter(TraceIdFilter())
    logger = logging.getLogger("swarm.test.fmt")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    # capture via a propagating record
    rec = logging.LogRecord("swarm.test.fmt", logging.INFO, "f", 1, "hello", None, None)
    handler.handle(rec)  # should not raise; trace_id resolved by filter
    assert rec.trace_id == "t-fmt"
