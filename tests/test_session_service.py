"""SessionService tests: structured state + events + SQLite persistence."""
from src.session.service import SessionService


def test_create_session(tmp_path):
    svc = SessionService(tmp_path / "swarm.db", tmp_path)
    sess = svc.create_session("default")
    assert sess.session_id
    assert sess.state == {}
    assert sess.events == []
    assert "sessions" in sess.work_dir


def test_get_session(tmp_path):
    svc = SessionService(tmp_path / "swarm.db", tmp_path)
    sess = svc.create_session()
    loaded = svc.get_session(sess.session_id)
    assert loaded is not None
    assert loaded.session_id == sess.session_id


def test_get_nonexistent(tmp_path):
    svc = SessionService(tmp_path / "swarm.db", tmp_path)
    assert svc.get_session("nope") is None


def test_append_event(tmp_path):
    svc = SessionService(tmp_path / "swarm.db", tmp_path)
    sess = svc.create_session()
    svc.append_event(sess.session_id, {"type": "user_message", "text": "hello"})
    svc.append_event(sess.session_id, {"type": "plan_created", "plan": {"x": 1}})

    loaded = svc.get_session(sess.session_id)
    assert len(loaded.events) == 2
    assert loaded.events[0]["type"] == "user_message"
    assert loaded.events[1]["plan"] == {"x": 1}
    # auto timestamp
    assert "timestamp" in loaded.events[0]


def test_update_state(tmp_path):
    svc = SessionService(tmp_path / "swarm.db", tmp_path)
    sess = svc.create_session()
    svc.update_state(sess.session_id, {"plan": {"api_contract": "GET /api/todos"}})
    svc.update_state(sess.session_id, {"artifacts": {"frontend": ["index.html"]}})

    loaded = svc.get_session(sess.session_id)
    assert loaded.state["plan"]["api_contract"] == "GET /api/todos"
    assert loaded.state["artifacts"]["frontend"] == ["index.html"]


def test_update_state_deep_merge(tmp_path):
    svc = SessionService(tmp_path / "swarm.db", tmp_path)
    sess = svc.create_session()
    svc.update_state(sess.session_id, {"plan": {"analysis": "A", "contract": "C"}})
    svc.update_state(sess.session_id, {"plan": {"analysis": "B"}})  # merge, not replace

    loaded = svc.get_session(sess.session_id)
    assert loaded.state["plan"]["analysis"] == "B"  # overwritten
    assert loaded.state["plan"]["contract"] == "C"  # preserved


def test_survives_restart(tmp_path):
    svc1 = SessionService(tmp_path / "swarm.db", tmp_path)
    sess = svc1.create_session()
    svc1.update_state(sess.session_id, {"plan": {"x": 1}})
    svc1.append_event(sess.session_id, {"type": "user_message", "text": "hi"})

    svc2 = SessionService(tmp_path / "swarm.db", tmp_path)  # "restart"
    loaded = svc2.get_session(sess.session_id)
    assert loaded is not None
    assert loaded.state["plan"]["x"] == 1
    assert len(loaded.events) == 1
    assert loaded.events[0]["text"] == "hi"


def test_append_event_nonexistent_returns_none(tmp_path):
    svc = SessionService(tmp_path / "swarm.db", tmp_path)
    assert svc.append_event("ghost", {"type": "x"}) is None


def test_update_state_nonexistent_returns_none(tmp_path):
    svc = SessionService(tmp_path / "swarm.db", tmp_path)
    assert svc.update_state("ghost", {"x": 1}) is None
