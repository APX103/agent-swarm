"""W3: workers can READ the shared task dir / sibling outputs (read-only), while
writes stay scoped to their own role dir. Lets agents build on each other's work."""
from pathlib import Path

from src.agents.worker import execute_file_tool


def test_read_shared_file_reads_sibling_output(monkeypatch, tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "api.py").write_text("API = 'code'")
    monkeypatch.setenv("SHARED_DIR", str(tmp_path))
    monkeypatch.setattr("src.agents.worker.AGENT_ROLE", "frontend-ux-pro")

    result = execute_file_tool("read_shared_file", {"path": "backend/api.py"})
    assert "API = 'code'" in result


def test_read_shared_file_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("SHARED_DIR", str(tmp_path))
    result = execute_file_tool("read_shared_file", {"path": "nope.txt"})
    assert "nope" in result.lower() or "not found" in result.lower()


def test_list_shared_lists_all_roles(monkeypatch, tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "api.py").write_text("x")
    (tmp_path / "_plan").mkdir()
    (tmp_path / "_plan" / "plan.md").write_text("p")
    monkeypatch.setenv("SHARED_DIR", str(tmp_path))

    result = execute_file_tool("list_shared", {})
    assert "backend/api.py" in result
    assert "_plan/plan.md" in result


def test_write_remains_scoped_to_own_role_dir(monkeypatch, tmp_path):
    """Writes must NOT escape to the shared root or sibling dirs."""
    monkeypatch.setenv("SHARED_DIR", str(tmp_path))
    monkeypatch.setattr("src.agents.worker.AGENT_ROLE", "frontend-ux-pro")

    execute_file_tool("write_file", {"path": "index.html", "content": "hi"})
    assert (tmp_path / "frontend" / "index.html").exists()   # own role dir
    assert not (tmp_path / "index.html").exists()            # not in shared root
