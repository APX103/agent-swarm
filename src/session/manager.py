"""Session manager — ties conversation history + work folder to a session_id.

A session persists across multiple /api/chat calls AND across process restarts:
- same session_id → same work_dir (agents keep writing to the same folder)
- same session_id → orchestrator remembers previous turns (messages retained)
- new session_id (or None) → fresh work_dir + empty history
- context persisted to _session/context.json (survives restart; restored on resume)
"""
from __future__ import annotations

import json
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
    """In-process session store with disk persistence.

    Context (messages + shared_context) is saved to ``_session/context.json``
    inside the session's work_dir. On resume after a restart, the context is
    loaded from disk automatically.
    """

    def __init__(self, base: str, store=None) -> None:
        self._base = Path(base)
        self._sessions: dict[str, SessionState] = {}
        self._store = store  # optional SQLiteStore

    # ── public API ────────────────────────────────────────────────────────────

    def get_or_create(
        self, session_id: Optional[str], tenant_id: str = "default"
    ) -> SessionState:
        """Return existing session (resume) or create a new one.

        Lookup order:
        1. In-memory cache (fast path for live sessions).
        2. Disk (``_session/context.json`` — survives process restart).
        3. Create fresh.
        """
        # 1. in-memory
        if session_id and session_id in self._sessions:
            logger.info("Resuming session %s from memory", session_id)
            return self._sessions[session_id]

        # 2. persistent store (SQLite or filesystem fallback)
        if session_id:
            loaded = self._load_persistent(session_id, tenant_id)
            if loaded is not None:
                self._sessions[session_id] = loaded
                logger.info("Restored session %s from disk (%d messages)", session_id, len(loaded.messages))
                return loaded

        # 3. new
        sid = session_id or str(uuid.uuid4())[:8]
        work_dir = self._base / "tenants" / tenant_id / "sessions" / sid
        work_dir.mkdir(parents=True, exist_ok=True)
        state = SessionState(session_id=sid, tenant_id=tenant_id, work_dir=work_dir)
        self._sessions[sid] = state
        logger.info("Created session %s (work_dir=%s)", sid, work_dir)
        return state

    def save(self, state: SessionState) -> None:
        """Persist session context (SQLite if available, else filesystem JSON)."""
        if self._store:
            self._store.save_session(
                state.session_id, state.tenant_id, str(state.work_dir),
                state.messages, state.shared_context, state.created_at,
            )
        else:
            self._save_to_disk(state)

    def get(self, session_id: str) -> Optional[SessionState]:
        return self._sessions.get(session_id)

    # ── disk persistence ──────────────────────────────────────────────────────

    def _load_persistent(self, session_id: str, tenant_id: str) -> Optional[SessionState]:
        """Load from SQLite (preferred) or filesystem JSON (fallback)."""
        if self._store:
            data = self._store.get_session(session_id)
            if data:
                work_dir = Path(data["work_dir"])
                return SessionState(
                    session_id=session_id,
                    tenant_id=data.get("tenant_id", tenant_id),
                    work_dir=work_dir,
                    messages=data.get("messages", []),
                    shared_context=data.get("shared_context", ""),
                    created_at=data.get("created_at", time.time()),
                )
        return self._load_from_disk(session_id, tenant_id)

    def _context_path(self, session_id: str, tenant_id: str) -> Path:
        return self._base / "tenants" / tenant_id / "sessions" / session_id / "_session" / "context.json"

    def _save_to_disk(self, state: SessionState) -> None:
        p = self._context_path(state.session_id, state.tenant_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "messages": state.messages,
            "shared_context": state.shared_context,
            "created_at": state.created_at,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_from_disk(self, session_id: str, tenant_id: str) -> Optional[SessionState]:
        p = self._context_path(session_id, tenant_id)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to load session context from %s", p, exc_info=True)
            return None
        work_dir = self._base / "tenants" / tenant_id / "sessions" / session_id
        return SessionState(
            session_id=session_id,
            tenant_id=tenant_id,
            work_dir=work_dir,
            messages=data.get("messages", []),
            shared_context=data.get("shared_context", ""),
            created_at=data.get("created_at", time.time()),
        )
