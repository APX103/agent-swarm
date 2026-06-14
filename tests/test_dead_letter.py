"""W10 tests: dead-letter store + query endpoint.

Failed orchestrations are recorded for inspection/replay. The store is bounded
(in-memory); a GET endpoint exposes recent entries.
"""
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.reliability.dead_letter import DeadLetterRecord, DeadLetterStore


def test_record_and_recent():
    s = DeadLetterStore(max_size=10)
    s.record(DeadLetterRecord(task_id="t1", tenant_id="ten", error="boom", user_message="hi"))
    recs = s.recent(5)
    assert len(recs) == 1
    assert recs[0].task_id == "t1"
    assert recs[0].error == "boom"


def test_store_is_bounded_evicting_oldest():
    s = DeadLetterStore(max_size=2)
    for i in range(5):
        s.record(DeadLetterRecord(task_id=f"t{i}", tenant_id="ten", error="e", user_message="m"))
    all_records = s.all()
    assert len(all_records) == 2
    assert all_records[-1].task_id == "t4"  # newest kept


def test_clear():
    s = DeadLetterStore()
    s.record(DeadLetterRecord(task_id="t", tenant_id="x", error="e", user_message="m"))
    s.clear()
    assert s.all() == []


def test_dead_letters_endpoint_returns_records():
    import src.api.routes as routes
    from src.api.routes import router

    routes.dead_letters.clear()
    routes.dead_letters.record(
        DeadLetterRecord(task_id="t1", tenant_id="ten", error="boom", user_message="hi")
    )

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/api/v1/dead-letters")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["task_id"] == "t1"
    assert data[0]["error"] == "boom"
