"""Session manager — ties conversation history + work folder to a session_id.

A session persists across multiple /api/chat calls:
- same session_id → same work_dir (agents keep writing to the same folder)
- same session_id → orchestrator remembers previous turns (messages retained)
- new session_id (or None) → fresh work_dir + empty history
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SessionState:
    """Per-session state: work folder + conversation history + shared context."""

    session_id: str
    tenant_id: str
    work_dir: Path
    messages: list[dict] = field(default_factory=list)
    shared_context: str = ""
    created_at: float = field(default_factory=time.time)


class SessionManager:
    """In-process session store. Maps session_id → SessionState."""

    def __init__(self, base: str) -> None:
        self._base = Path(base)
        self._sessions: dict[str, SessionState] = {}

    def get_or_create(
        self, session_id: Optional[str], tenant_id: str = "default"
    ) -> SessionState:
        """Return existing session (resume) or create a new one.

        - session_id provided + known → resume (same work_dir + history).
        - session_id provided + unknown → create with that id.
        - session_id None → create with a fresh id.
        """
        if session_id and session_id in self._sessions:
            logger.info("Resuming session %s (work_dir=%s)", session_id, self._sessions[session_id].work_dir)
            return self._sessions[session_id]

        sid = session_id or str(uuid.uuid4())[:8]
        work_dir = self._base / "tenants" / tenant_id / "sessions" / sid
        work_dir.mkdir(parents=True, exist_ok=True)

        state = SessionState(
            session_id=sid,
            tenant_id=tenant_id,
            work_dir=work_dir,
        )
        self._sessions[sid] = state
        logger.info("Created session %s (work_dir=%s)", sid, work_dir)
        return state

    def get(self, session_id: str) -> Optional[SessionState]:
        return self._sessions.get(session_id)
