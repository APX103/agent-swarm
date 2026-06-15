"""SQLiteStore tests: tasks + sessions survive across instances (= restart)."""
import time

from src.storage.sqlite_store import SQLiteStore


def test_save_and_get_task(tmp_path):
    store = SQLiteStore(tmp_path / "swarm.db")
    store.save_task(
        task_id="t1", tenant_id="default", user_message="hello",
        status="completed", result="done", artifacts=["frontend/index.html"],
        work_dir="/tmp/work",
    )
    t = store.get_task("t1")
    assert t is not None
    assert t["task_id"] == "t1"
    assert t["status"] == "completed"
    assert t["artifacts"] == ["frontend/index.html"]
    assert t["result"] == "done"


def test_task_survives_new_instance(tmp_path):
    """Simulate restart: new SQLiteStore, same db file → task still there."""
    store1 = SQLiteStore(tmp_path / "swarm.db")
    store1.save_task(task_id="persist-test", status="running", work_dir="/tmp/x")

    store2 = SQLiteStore(tmp_path / "swarm.db")  # "restart"
    t = store2.get_task("persist-test")
    assert t is not None
    assert t["status"] == "running"


def test_list_tasks(tmp_path):
    store = SQLiteStore(tmp_path / "swarm.db")
    store.save_task(task_id="t1", tenant_id="default", status="completed")
    store.save_task(task_id="t2", tenant_id="default", status="running")
    store.save_task(task_id="t3", tenant_id="other", status="completed")
    all_tasks = store.list_tasks()
    assert len(all_tasks) == 3
    default_tasks = store.list_tasks("default")
    assert len(default_tasks) == 2


def test_save_and_get_session(tmp_path):
    store = SQLiteStore(tmp_path / "swarm.db")
    store.save_session(
        session_id="s1", tenant_id="default", work_dir="/tmp/s1",
        messages=[{"role": "user", "content": "hi"}],
        shared_context="PLAN", created_at=time.time(),
    )
    s = store.get_session("s1")
    assert s is not None
    assert s["session_id"] == "s1"
    assert s["messages"] == [{"role": "user", "content": "hi"}]
    assert s["shared_context"] == "PLAN"


def test_session_survives_new_instance(tmp_path):
    store1 = SQLiteStore(tmp_path / "swarm.db")
    store1.save_session(
        session_id="persist-sess", tenant_id="default", work_dir="/tmp/ps",
        messages=[{"role": "user", "content": "remember me"}],
        shared_context="CTX", created_at=time.time(),
    )
    store2 = SQLiteStore(tmp_path / "swarm.db")
    s = store2.get_session("persist-sess")
    assert s is not None
    assert s["messages"] == [{"role": "user", "content": "remember me"}]


def test_get_nonexistent_returns_none(tmp_path):
    store = SQLiteStore(tmp_path / "swarm.db")
    assert store.get_task("nope") is None
    assert store.get_session("nope") is None
