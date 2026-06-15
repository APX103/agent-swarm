"""Event-First Session models — structured state + append-only events."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Session:
    """Structured session: state dict + event log (ADK SessionService style).

    - state: structured context that agents read/write (plan, artifacts manifest, decisions).
    - events: append-only audit log of semantic events (what happened, not raw LLM messages).
    """

    session_id: str
    tenant_id: str = "default"
    work_dir: str = ""
    state: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
