"""SessionService tests: async structured state + events + SQLite persistence."""
import pytest

from src.session.service import SessionService


@pytest.mark.asyncio
async def test_create_session(tmp_path):
    svc = SessionService(tmp_path / "swarm.db", tmp_path)
    sess = await svc.create_session("default")
    assert sess.session_id
    assert sess.state == {}
    assert sess.events == []
    assert "sessions" in sess.work_dir


@pytest.mark.asyncio
async def test_get_session(tmp_path):
    svc = SessionService(tmp_path / "swarm.db", tmp_path)
    sess = await svc.create_session()
    loaded = await svc.get_session(sess.session_id)
    assert loaded is not None
    assert loaded.session_id == sess.session_id


@pytest.mark.asyncio
async def test_get_nonexistent(tmp_path):
    svc = SessionService(tmp_path / "swarm.db", tmp_path)
    assert await svc.get_session("nope") is None


@pytest.mark.asyncio
async def test_append_event(tmp_path):
    svc = SessionService(tmp_path / "swarm.db", tmp_path)
    sess = await svc.create_session()
    await svc.append_event(sess.session_id, {"type": "user_message", "text": "hello"})
    await svc.append_event(sess.session_id, {"type": "plan_created", "plan": {"x": 1}})

    loaded = await svc.get_session(sess.session_id)
    assert len(loaded.events) == 2
    assert loaded.events[0]["type"] == "user_message"
    assert loaded.events[1]["plan"] == {"x": 1}
    # auto timestamp
    assert "timestamp" in loaded.events[0]


@pytest.mark.asyncio
async def test_update_state(tmp_path):
    svc = SessionService(tmp_path / "swarm.db", tmp_path)
    sess = await svc.create_session()
    await svc.update_state(sess.session_id, {"plan": {"api_contract": "GET /api/todos"}})
    await svc.update_state(sess.session_id, {"artifacts": {"frontend": ["index.html"]}})

    loaded = await svc.get_session(sess.session_id)
    assert loaded.state["plan"]["api_contract"] == "GET /api/todos"
    assert loaded.state["artifacts"]["frontend"] == ["index.html"]


@pytest.mark.asyncio
async def test_update_state_deep_merge(tmp_path):
    svc = SessionService(tmp_path / "swarm.db", tmp_path)
    sess = await svc.create_session()
    await svc.update_state(sess.session_id, {"plan": {"analysis": "A", "contract": "C"}})
    await svc.update_state(sess.session_id, {"plan": {"analysis": "B"}})  # merge, not replace

    loaded = await svc.get_session(sess.session_id)
    assert loaded.state["plan"]["analysis"] == "B"  # overwritten
    assert loaded.state["plan"]["contract"] == "C"  # preserved


@pytest.mark.asyncio
async def test_survives_restart(tmp_path):
    svc1 = SessionService(tmp_path / "swarm.db", tmp_path)
    sess = await svc1.create_session()
    await svc1.update_state(sess.session_id, {"plan": {"x": 1}})
    await svc1.append_event(sess.session_id, {"type": "user_message", "text": "hi"})

    svc2 = SessionService(tmp_path / "swarm.db", tmp_path)  # "restart"
    loaded = await svc2.get_session(sess.session_id)
    assert loaded is not None
    assert loaded.state["plan"]["x"] == 1
    assert len(loaded.events) == 1
    assert loaded.events[0]["text"] == "hi"


@pytest.mark.asyncio
async def test_append_event_nonexistent_returns_none(tmp_path):
    svc = SessionService(tmp_path / "swarm.db", tmp_path)
    assert await svc.append_event("ghost", {"type": "x"}) is None


@pytest.mark.asyncio
async def test_update_state_nonexistent_returns_none(tmp_path):
    svc = SessionService(tmp_path / "swarm.db", tmp_path)
    assert await svc.update_state("ghost", {"x": 1}) is None


@pytest.mark.asyncio
async def test_get_or_create_with_id(tmp_path):
    svc = SessionService(tmp_path / "swarm.db", tmp_path)
    s1 = await svc.get_or_create_with_id("fixed-id", "default")
    assert s1.session_id == "fixed-id"
    # second call returns the same session (idempotent)
    s2 = await svc.get_or_create_with_id("fixed-id", "default")
    assert s2.session_id == "fixed-id"
