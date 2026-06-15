"""Session manager tests: work-folder binding + resume semantics."""
import pytest

from src.session.manager import SessionManager, SessionState


def test_new_session_creates_work_dir(tmp_path):
    sm = SessionManager(str(tmp_path))
    s = sm.get_or_create(None, "default")
    assert s.session_id
    assert s.work_dir.exists()
    assert "sessions" in str(s.work_dir)
    assert s.messages == []
    assert s.shared_context == ""


def test_resume_returns_same_object(tmp_path):
    sm = SessionManager(str(tmp_path))
    s1 = sm.get_or_create(None, "default")
    s1.messages.append({"role": "user", "content": "hello"})
    s1.shared_context = "PLAN"

    s2 = sm.get_or_create(s1.session_id, "default")
    assert s2 is s1
    assert s2.messages == [{"role": "user", "content": "hello"}]
    assert s2.shared_context == "PLAN"
    assert s2.work_dir == s1.work_dir


def test_different_sessions_different_dirs(tmp_path):
    sm = SessionManager(str(tmp_path))
    s1 = sm.get_or_create(None, "default")
    s2 = sm.get_or_create(None, "default")
    assert s1.session_id != s2.session_id
    assert s1.work_dir != s2.work_dir


def test_explicit_session_id_creates_then_resumes(tmp_path):
    sm = SessionManager(str(tmp_path))
    s1 = sm.get_or_create("my-session", "default")
    assert s1.session_id == "my-session"
    s2 = sm.get_or_create("my-session", "default")
    assert s2 is s1


def test_tenant_isolation(tmp_path):
    sm = SessionManager(str(tmp_path))
    s1 = sm.get_or_create(None, "tenant-a")
    s2 = sm.get_or_create(None, "tenant-b")
    assert "tenant-a" in str(s1.work_dir)
    assert "tenant-b" in str(s2.work_dir)
    assert s1.work_dir != s2.work_dir


def test_get_unknown_returns_none(tmp_path):
    sm = SessionManager(str(tmp_path))
    assert sm.get("nonexistent") is None


# ── disk persistence (survive restart) ─────────────────────────────────────────


def test_save_persists_context_to_disk(tmp_path):
    sm = SessionManager(str(tmp_path))
    s = sm.get_or_create("persist-test", "default")
    s.messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    s.shared_context = "THE PLAN"
    sm.save(s)
    ctx_file = s.work_dir / "_session" / "context.json"
    assert ctx_file.exists()


def test_restore_from_disk_after_restart(tmp_path):
    """Simulate process restart: new SessionManager, same base dir → context restored."""
    sm1 = SessionManager(str(tmp_path))
    s1 = sm1.get_or_create("resume-test", "default")
    s1.messages = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "done"}]
    s1.shared_context = "PLAN"
    sm1.save(s1)

    sm2 = SessionManager(str(tmp_path))  # fresh instance = "restart"
    s2 = sm2.get_or_create("resume-test", "default")
    assert s2.messages == s1.messages
    assert s2.shared_context == "PLAN"
    assert s2.work_dir == s1.work_dir


def test_unknown_session_no_disk_file_creates_new(tmp_path):
    sm = SessionManager(str(tmp_path))
    s = sm.get_or_create("never-saved", "default")
    assert s.messages == []
    assert s.shared_context == ""
