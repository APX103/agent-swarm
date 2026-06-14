"""W9 tests: /api/chat idempotency via Idempotency-Key header.

A repeated request with the same key replays the existing task instead of creating
a new orchestration. Absence of a key always creates a new task.
"""
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("openai", MagicMock())

from src.api.models import TaskStatus  # noqa: E402
from src.api.routes import _idempotency_index, router, set_deps  # noqa: E402


def setup_function():
    _idempotency_index.clear()


def _tm_with_distinct_ids():
    tm = MagicMock()
    counter = {"i": 0}

    def make(*a, **kw):
        counter["i"] += 1
        tid = f"task-{counter['i']}"
        return MagicMock(
            task_id=tid, tenant_id="default", status=TaskStatus.RUNNING,
            result=None, artifacts=[], work_dir=None, subscribe=lambda cb: None,
        )

    tm.create_task = AsyncMock(side_effect=make)
    tm.update_status = AsyncMock()
    tm.complete_task = AsyncMock()
    tm.fail_task = AsyncMock()
    tm.get_task = MagicMock(
        side_effect=lambda tid: MagicMock(
            task_id=tid, status=TaskStatus.RUNNING, result=None, artifacts=[]
        )
    )
    return tm


def _client(tm):
    @asynccontextmanager
    async def lifespan(app):
        yield

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    set_deps(MagicMock(), tm, MagicMock())
    return TestClient(app, raise_server_exceptions=False)


def test_same_idempotency_key_replays_existing_task():
    tm = _tm_with_distinct_ids()
    client = _client(tm)
    headers = {"Idempotency-Key": "abc"}

    r1 = client.post("/api/chat", json={"message": "hi"}, headers=headers)
    r2 = client.post("/api/chat", json={"message": "hi"}, headers=headers)

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["task_id"] == r2.json()["task_id"]  # replayed
    assert tm.create_task.await_count == 1  # created only once


def test_no_key_always_creates_new_task():
    tm = _tm_with_distinct_ids()
    client = _client(tm)

    r1 = client.post("/api/chat", json={"message": "hi"})
    r2 = client.post("/api/chat", json={"message": "hi"})

    assert r1.json()["task_id"] != r2.json()["task_id"]
    assert tm.create_task.await_count == 2


def test_different_keys_yield_different_tasks():
    tm = _tm_with_distinct_ids()
    client = _client(tm)

    r1 = client.post("/api/chat", json={"message": "hi"}, headers={"Idempotency-Key": "k1"})
    r2 = client.post("/api/chat", json={"message": "hi"}, headers={"Idempotency-Key": "k2"})

    assert r1.json()["task_id"] != r2.json()["task_id"]
    assert tm.create_task.await_count == 2
