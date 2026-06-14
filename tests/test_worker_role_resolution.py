"""W2: worker role resolution prefers env-injected definitions (so a config-defined
role reaches the worker without editing worker.py), falling back to built-ins."""
import json

from src.agents.worker import AGENT_CARDS, SYSTEM_PROMPTS, resolve_card, resolve_prompt


def test_resolve_card_builtin(monkeypatch):
    monkeypatch.delenv("WORKER_ROLE_CARD", raising=False)
    assert resolve_card("frontend-ux-pro")["name"] == "Frontend UX Pro"


def test_resolve_card_env_overrides_builtin(monkeypatch):
    monkeypatch.setenv("WORKER_ROLE_CARD", json.dumps({"name": "Custom Role", "skills": []}))
    assert resolve_card("frontend-ux-pro")["name"] == "Custom Role"


def test_resolve_card_unknown_falls_back_general(monkeypatch):
    monkeypatch.delenv("WORKER_ROLE_CARD", raising=False)
    assert resolve_card("does-not-exist")["name"] == AGENT_CARDS["general-agent"]["name"]


def test_resolve_prompt_env_overrides(monkeypatch):
    monkeypatch.setenv("AGENT_SYSTEM_PROMPT", "you are a custom role")
    assert resolve_prompt("frontend-ux-pro") == "you are a custom role"


def test_resolve_prompt_builtin(monkeypatch):
    monkeypatch.delenv("AGENT_SYSTEM_PROMPT", raising=False)
    assert resolve_prompt("backend-engineer") == SYSTEM_PROMPTS["backend-engineer"]


def test_resolve_prompt_unknown_falls_back_general(monkeypatch):
    monkeypatch.delenv("AGENT_SYSTEM_PROMPT", raising=False)
    assert resolve_prompt("nope") == SYSTEM_PROMPTS["general-agent"]
