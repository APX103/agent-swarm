"""W4: enforced review — every dispatched agent must have produced artifacts before
finalize can complete. review_artifacts is the testable core."""
from src.orchestrator.orchestrator import review_artifacts


def test_review_passes_when_all_agents_produced(tmp_path):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "index.html").write_text("x")
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "api.py").write_text("y")
    r = review_artifacts(["frontend-ux-pro", "backend-engineer"], tmp_path)
    assert r["passed"] is True
    assert r["missing"] == []


def test_review_fails_when_an_agent_produced_nothing(tmp_path):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "index.html").write_text("x")
    r = review_artifacts(["frontend-ux-pro", "backend-engineer"], tmp_path)
    assert r["passed"] is False
    assert r["missing"] == ["backend-engineer"]


def test_review_ignores_plan_dir_only(tmp_path):
    # only _plan exists, no role output -> fail for the dispatched agent
    (tmp_path / "_plan").mkdir()
    (tmp_path / "_plan" / "plan.md").write_text("p")
    r = review_artifacts(["general-agent"], tmp_path)
    assert r["passed"] is False


def test_review_empty_dispatched_passes(tmp_path):
    # nothing dispatched -> nothing to fail on
    r = review_artifacts([], tmp_path)
    assert r["passed"] is True
