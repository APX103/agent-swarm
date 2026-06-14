"""Per-task trace id via contextvars + a logging filter.

The trace id is set at the request boundary (/api/chat) and propagates through
asyncio tasks (each task copies the context), so every log record emitted while
serving a task — orchestrator, dispatcher, backend — carries the same id. A
``TraceIdFilter`` attached to a handler injects ``record.trace_id`` so formatters
can render ``%(trace_id)s``.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Optional

_trace_id: ContextVar[Optional[str]] = ContextVar("swarm_trace_id", default=None)


def set_trace_id(trace_id: Optional[str]) -> None:
    """Set the trace id for the current async context."""
    _trace_id.set(trace_id)


def get_trace_id() -> Optional[str]:
    """Return the trace id for the current async context (or None)."""
    return _trace_id.get()


class TraceIdFilter(logging.Filter):
    """Inject the current trace id into every log record as ``record.trace_id``."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id() or "-"
        return True
