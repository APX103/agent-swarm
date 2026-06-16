"""SQLite-backed persistence for tasks + sessions (Python built-in sqlite3, zero deps).

Write-through cache pattern: managers keep in-memory dicts for fast reads,
but every mutation also writes to SQLite. On restart (empty cache), data is
loaded from SQLite automatically.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class SQLiteStore:
    """SQLite persistence layer for tasks, sessions."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _init_tables(self) -> None:
        c = self._conn()
        try:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id     TEXT PRIMARY KEY,
                    tenant_id   TEXT NOT NULL DEFAULT 'default',
                    session_id  TEXT,
                    user_message TEXT,
                    status      TEXT NOT NULL DEFAULT 'created',
                    result      TEXT,
                    artifacts   TEXT DEFAULT '[]',
                    work_dir    TEXT,
                    created_at  TEXT,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id     TEXT PRIMARY KEY,
                    tenant_id      TEXT NOT NULL DEFAULT 'default',
                    work_dir       TEXT NOT NULL,
                    messages       TEXT DEFAULT '[]',
                    shared_context TEXT DEFAULT '',
                    created_at     REAL
                );
                """
            )
            c.commit()
        finally:
            c.close()

    # ── tasks ──────────────────────────────────────────────────────────────────

    def save_task(
        self,
        task_id: str,
        tenant_id: str = "default",
        session_id: Optional[str] = None,
        user_message: str = "",
        status: str = "created",
        result: Optional[str] = None,
        artifacts: Optional[list[str]] = None,
        work_dir: Optional[str] = None,
        created_at: Optional[str] = None,
        completed_at: Optional[str] = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO tasks
                   (task_id, tenant_id, session_id, user_message, status, result,
                    artifacts, work_dir, created_at, completed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, tenant_id, session_id, user_message, status, result,
                    json.dumps(artifacts or []), work_dir,
                    created_at or datetime.now().isoformat(), completed_at,
                ),
            )

    def get_task(self, task_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["artifacts"] = json.loads(d.get("artifacts", "[]"))
            return d

    def list_tasks(self, tenant_id: Optional[str] = None) -> list[dict]:
        with self._conn() as c:
            if tenant_id:
                rows = c.execute(
                    "SELECT * FROM tasks WHERE tenant_id=? ORDER BY created_at DESC",
                    (tenant_id,),
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
            result = []
            for row in rows:
                d = dict(row)
                d["artifacts"] = json.loads(d.get("artifacts", "[]"))
                result.append(d)
            return result

    # ── sessions ───────────────────────────────────────────────────────────────

    def save_session(
        self,
        session_id: str,
        tenant_id: str,
        work_dir: str,
        messages: list[dict],
        shared_context: str,
        created_at: float,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO sessions
                   (session_id, tenant_id, work_dir, messages, shared_context, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    session_id, tenant_id, str(work_dir),
                    json.dumps(messages, ensure_ascii=False),
                    shared_context, created_at,
                ),
            )

    def get_session(self, session_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["messages"] = json.loads(d.get("messages", "[]"))
            return d

    def list_session_ids(self) -> list[str]:
        with self._conn() as c:
            return [r["session_id"] for r in c.execute("SELECT session_id FROM sessions").fetchall()]
