"""SessionService — async session store with structured state + events.

SQLite-backed (zero deps). All public methods are ``async``; every blocking
``sqlite3`` call is run off the event loop via :func:`asyncio.to_thread` so a
slow disk can't stall request handling. Each session has:
- state: a dict that agents read/write (plan, artifacts, decisions).
- events: an append-only log of semantic events.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Optional

from src.session.models import Session

logger = logging.getLogger(__name__)


class SessionService:
    """Async SQLite-backed session service with structured state + events."""

    def __init__(self, db_path: str | Path, base_dir: str | Path) -> None:
        self._db_path = str(db_path)
        self._base = Path(base_dir)
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_lock = asyncio.Lock()  # protects the _locks dict itself
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_table()

    async def _get_lock(self, session_id: str) -> asyncio.Lock:
        """Per-session lock to serialize read-modify-write (prevent lost updates).

        Uses double-check locking to handle concurrent creation, and cleans up
        locks for sessions that no longer exist to prevent unbounded growth.
        """
        # Fast path: check without acquiring the dict lock
        lock = self._locks.get(session_id)
        if lock is not None:
            return lock

        async with self._locks_lock:
            # Double-check after acquiring the lock
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock

    async def _cleanup_stale_locks(self) -> int:
        """Remove locks for sessions that no longer exist in the database.

        Returns the number of locks removed.
        """
        if not self._locks:
            return 0

        # Get all known session IDs from DB
        rows = await asyncio.to_thread(self._list_session_ids_sync)
        known_ids = {row[0] for row in rows}

        async with self._locks_lock:
            stale = [sid for sid in self._locks if sid not in known_ids]
            for sid in stale:
                del self._locks[sid]
            return len(stale)

    def _list_session_ids_sync(self) -> list[tuple]:
        with self._conn() as c:
            return c.execute("SELECT session_id FROM sessions_v2").fetchall()

    # ── low-level sync sqlite helpers (always run in a worker thread) ─────────

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        c = sqlite3.connect(self._db_path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def _init_table(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS sessions_v2 (
                    session_id  TEXT PRIMARY KEY,
                    tenant_id   TEXT NOT NULL DEFAULT 'default',
                    work_dir    TEXT NOT NULL,
                    state       TEXT DEFAULT '{}',
                    events      TEXT DEFAULT '[]',
                    created_at  REAL
                )"""
            )
            c.commit()

    # ── public async API ─────────────────────────────────────────────────────

    async def get_or_create_with_id(self, session_id: str, tenant_id: str = "default") -> Session:
        """Get existing session or create with the given session_id (for alignment with SessionManager)."""
        existing = await self.get_session(session_id)
        if existing:
            return existing
        work_dir = str(self._base / "tenants" / tenant_id / "sessions" / session_id)
        await asyncio.to_thread(lambda: Path(work_dir).mkdir(parents=True, exist_ok=True))
        sess = Session(session_id=session_id, tenant_id=tenant_id, work_dir=work_dir)
        await self._save(sess)
        return sess

    async def create_session(self, tenant_id: str = "default") -> Session:
        """Create a fresh session with empty state + events."""
        sid = str(uuid.uuid4())[:8]
        work_dir = str(self._base / "tenants" / tenant_id / "sessions" / sid)
        await asyncio.to_thread(lambda: Path(work_dir).mkdir(parents=True, exist_ok=True))

        sess = Session(session_id=sid, tenant_id=tenant_id, work_dir=work_dir)
        await self._save(sess)
        logger.info("Created session %s (work_dir=%s)", sid, work_dir)
        return sess

    async def get_session(self, session_id: str) -> Optional[Session]:
        """Load session from SQLite (or None if not found)."""
        row = await asyncio.to_thread(self._get_session_row, session_id)
        if row is None:
            return None
        return self._row_to_session(row)

    def _get_session_row(self, session_id: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM sessions_v2 WHERE session_id=?", (session_id,)
            ).fetchone()

    async def list_sessions(
        self, tenant_id: Optional[str] = None, limit: int = 100
    ) -> list[Session]:
        """List sessions from SQLite, newest first."""
        return await asyncio.to_thread(self._list_sessions_sync, tenant_id, limit)

    def _list_sessions_sync(
        self, tenant_id: Optional[str], limit: int
    ) -> list[Session]:
        with self._conn() as c:
            if tenant_id:
                rows = c.execute(
                    "SELECT * FROM sessions_v2 WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
                    (tenant_id, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM sessions_v2 ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._row_to_session(row) for row in rows]

    async def append_event(self, session_id: str, event: dict[str, Any]) -> Optional[Session]:
        """Append an event to the session's log + persist. Returns updated session.

        Serialized per-session to prevent concurrent append_event calls from
        losing events (read-modify-write under lock).
        """
        lock = await self._get_lock(session_id)
        async with lock:
            sess = await self.get_session(session_id)
            if sess is None:
                return None
            event.setdefault("timestamp", time.time())
            sess.events.append(event)
            await self._save(sess)
            return sess

    async def update_state(self, session_id: str, delta: dict[str, Any]) -> Optional[Session]:
        """Deep-merge delta into session.state + persist. Returns updated session.

        Serialized per-session to prevent concurrent update_state calls from
        clobbering each other's writes.
        """
        lock = await self._get_lock(session_id)
        async with lock:
            sess = await self.get_session(session_id)
            if sess is None:
                return None
            _deep_merge(sess.state, delta)
            await self._save(sess)
            return sess

    # ── internals ──────────────────────────────────────────────────────────────

    async def _save(self, sess: Session) -> None:
        await asyncio.to_thread(self._save_sync, sess)

    def _save_sync(self, sess: Session) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO sessions_v2
                   (session_id, tenant_id, work_dir, state, events, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    sess.session_id, sess.tenant_id, sess.work_dir,
                    json.dumps(sess.state, ensure_ascii=False),
                    json.dumps(sess.events, ensure_ascii=False),
                    sess.created_at,
                ),
            )

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Session:
        return Session(
            session_id=row["session_id"],
            tenant_id=row["tenant_id"],
            work_dir=row["work_dir"],
            state=json.loads(row["state"] or "{}"),
            events=json.loads(row["events"] or "[]"),
            created_at=row["created_at"],
        )


def _deep_merge(target: dict, delta: dict) -> None:
    """Recursively merge delta into target (in-place)."""
    for k, v in delta.items():
        if k in target and isinstance(target[k], dict) and isinstance(v, dict):
            _deep_merge(target[k], v)
        else:
            target[k] = v
