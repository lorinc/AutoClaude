import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _run_main(monkeypatch, tmp_path, env_vars: dict, *, write_registry=True, write_key=True):
    """Helper: patch REPO_ROOT, set env vars, call main()."""
    if write_registry:
        (tmp_path / "project_registry.json").write_text("[]")
    if write_key:
        env_vars.setdefault("ANTHROPIC_API_KEY", "test-key")

    from orchestrator import __main__ as m
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)

    with patch.dict(os.environ, env_vars, clear=False):
        yield


# ---------------------------------------------------------------------------
# Fail-fast: missing registry
# ---------------------------------------------------------------------------

def test_missing_registry_exits(monkeypatch, tmp_path):
    from orchestrator import __main__ as m
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
        with pytest.raises(SystemExit) as exc:
            m._check_prerequisites(tmp_path / "project_registry.json")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Fail-fast: missing API key
# ---------------------------------------------------------------------------

def test_missing_api_key_exits(monkeypatch, tmp_path):
    reg = tmp_path / "project_registry.json"
    reg.write_text("[]")
    from orchestrator import __main__ as m
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit) as exc:
            m._check_prerequisites(reg)
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Fail-fast: both present — no exit
# ---------------------------------------------------------------------------

def test_prerequisites_pass(tmp_path):
    reg = tmp_path / "project_registry.json"
    reg.write_text("[]")
    from orchestrator import __main__ as m
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
        m._check_prerequisites(reg)  # must not raise


# ---------------------------------------------------------------------------
# _log_startup: runs without error on empty registry
# ---------------------------------------------------------------------------

def test_log_startup_empty_registry(monkeypatch, tmp_path):
    reg = tmp_path / "project_registry.json"
    reg.write_text("[]")
    from orchestrator import __main__ as m
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    m._log_startup(reg, dry_run=True, env="dev")  # must not raise


# ---------------------------------------------------------------------------
# main(): starts scheduler with correct params from env
# ---------------------------------------------------------------------------

def test_main_starts_scheduler(monkeypatch, tmp_path):
    reg = tmp_path / "project_registry.json"
    reg.write_text("[]")
    (tmp_path / "init_prompt.md").write_text("you are a senior architect")

    from orchestrator import __main__ as m
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)

    captured = {}

    class FakeScheduler:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self):
            pass  # don't loop

    env = {
        "ANTHROPIC_API_KEY": "test-key",
        "DRY_RUN": "true",
        "ORCHESTRATOR_ENV": "staging",
        "POLL_INTERVAL": "30",
        "TURN_LIMIT": "10",
        "MAX_RETRIES": "2",
    }
    with patch.dict(os.environ, env, clear=False), \
         patch("orchestrator.scheduler.Scheduler", FakeScheduler):
        m.main()

    assert captured["dry_run"] is True
    assert captured["poll_interval"] == 30
    assert captured["turn_limit"] == 10
    assert captured["max_retries"] == 2
    assert captured["init_prompt"] == "you are a senior architect"
    assert captured["registry_path"] == reg


def test_main_defaults(monkeypatch, tmp_path):
    reg = tmp_path / "project_registry.json"
    reg.write_text("[]")

    from orchestrator import __main__ as m
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)

    captured = {}

    class FakeScheduler:
        def __init__(self, **kwargs):
            captured.update(kwargs)
        def run(self):
            pass

    env = {"ANTHROPIC_API_KEY": "test-key"}
    # Remove override vars so defaults kick in
    clean_env = {k: v for k, v in os.environ.items()
                 if k not in ("DRY_RUN", "POLL_INTERVAL", "TURN_LIMIT", "MAX_RETRIES", "ORCHESTRATOR_ENV")}
    clean_env.update(env)
    with patch.dict(os.environ, clean_env, clear=True), \
         patch("orchestrator.scheduler.Scheduler", FakeScheduler):
        m.main()

    assert captured["dry_run"] is False
    assert captured["poll_interval"] == 60
    assert captured["turn_limit"] == 15
    assert captured["max_retries"] == 3
