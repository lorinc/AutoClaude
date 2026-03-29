import os
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.task_runner import (
    SessionResult,
    _count_tool_uses,
    _kill_process_group,
    build_invocation,
    infer_branch_type,
    move_task,
    run_explore_session,
    run_fix_session,
    run_primary_session,
    run_session,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def git_repo(tmp_path):
    cfg = {"check": True, "capture_output": True}
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], **cfg)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], **cfg)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], **cfg)
    (tmp_path / "devlog.md").write_text("")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], **cfg)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], **cfg)
    return tmp_path


@pytest.fixture
def task_repo(git_repo):
    for d in ("pending", "active", "done"):
        (git_repo / "tasks" / d).mkdir(parents=True)
    (git_repo / "tasks" / "pending" / "my-task.md").write_text("# Task\n## Goal\n...")
    subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-m", "add task"], check=True, capture_output=True)
    return git_repo


# ---------------------------------------------------------------------------
# infer_branch_type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug,expected", [
    ("fix-login-bug",    "fix"),
    ("hotfix-crash",     "fix"),
    ("bug-in-auth",      "fix"),
    ("chore-bump-deps",  "chore"),
    ("refactor-models",  "chore"),
    ("add-rate-limit",   "feature"),
    ("new-dashboard",    "feature"),
    ("",                 "feature"),
])
def test_infer_branch_type(slug, expected):
    assert infer_branch_type(slug) == expected


# ---------------------------------------------------------------------------
# move_task
# ---------------------------------------------------------------------------

def test_move_task(task_repo):
    result = move_task(task_repo, "my-task", "pending", "active")
    assert result == task_repo / "tasks" / "active" / "my-task.md"
    assert result.exists()
    assert not (task_repo / "tasks" / "pending" / "my-task.md").exists()


# ---------------------------------------------------------------------------
# build_invocation
# ---------------------------------------------------------------------------

def test_build_invocation_structure():
    inv = build_invocation(5.0, "system prompt here")
    assert inv[0] == "claude"
    assert "-p" in inv
    assert "--dangerously-skip-permissions" in inv
    assert "--verbose" in inv
    assert "--max-budget-usd" in inv
    assert "5.0" in inv
    assert "--output-format" in inv
    assert "stream-json" in inv
    assert "--append-system-prompt" in inv
    assert "system prompt here" in inv


def test_build_invocation_no_system_prompt():
    inv = build_invocation(5.0, "")
    assert "--append-system-prompt" not in inv


def test_build_invocation_custom_model():
    inv = build_invocation(5.0, "", model="claude-haiku-4-5-20251001")
    assert "--model" in inv
    assert "claude-haiku-4-5-20251001" in inv


# ---------------------------------------------------------------------------
# _count_tool_uses
# ---------------------------------------------------------------------------

def test_count_tool_uses_assistant_with_tool():
    line = '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"1","name":"Bash","input":{}}]}}'
    assert _count_tool_uses(line) == 1


def test_count_tool_uses_multiple():
    line = '{"type":"assistant","message":{"content":[{"type":"tool_use"},{"type":"text"},{"type":"tool_use"}]}}'
    assert _count_tool_uses(line) == 2


def test_count_tool_uses_non_assistant():
    line = '{"type":"system","message":{"content":[{"type":"tool_use"}]}}'
    assert _count_tool_uses(line) == 0


def test_count_tool_uses_invalid_json():
    assert _count_tool_uses("not json") == 0


def test_count_tool_uses_no_tools():
    line = '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}'
    assert _count_tool_uses(line) == 0


# ---------------------------------------------------------------------------
# run_session
# ---------------------------------------------------------------------------

class _MockProcess:
    def __init__(self, lines: list[str], returncode: int = 0):
        self.stdout = iter(lines)
        self.returncode = returncode
        self.pid = 12345
        self.killed_with: list[int] = []

    def wait(self, timeout=None):
        pass

    def kill(self):
        pass


def test_run_session_pass(tmp_path):
    lines = [
        '{"type":"assistant","message":{"content":[{"type":"text","text":"done"}]}}',
        '{"type":"result","subtype":"success","total_cost_usd":0.05,"num_turns":1}',
    ]
    mock_proc = _MockProcess(lines, returncode=0)
    with patch("orchestrator.task_runner.subprocess.Popen", return_value=mock_proc):
        result = run_session(tmp_path, ["claude", "-p"], "my-task", turn_limit=15)
    assert result.outcome == "pass"
    assert result.exit_code == 0
    assert result.cost_usd == pytest.approx(0.05)


def test_run_session_fail(tmp_path):
    mock_proc = _MockProcess([], returncode=1)
    with patch("orchestrator.task_runner.subprocess.Popen", return_value=mock_proc):
        result = run_session(tmp_path, ["claude", "-p"], "my-task", turn_limit=15)
    assert result.outcome == "fail"
    assert result.exit_code == 1
    assert result.cost_usd == 0.0


def test_run_session_stuck_at_turn_limit(tmp_path):
    tool_line = '{"type":"assistant","message":{"content":[{"type":"tool_use"}]}}'
    lines = [tool_line] * 20
    mock_proc = _MockProcess(lines, returncode=-15)
    pgid_calls: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        pgid_calls.append((pgid, sig))

    def fake_getpgid(pid):
        return pid  # pgid == pid

    with patch("orchestrator.task_runner.subprocess.Popen", return_value=mock_proc), \
         patch("orchestrator.task_runner.os.killpg", side_effect=fake_killpg), \
         patch("orchestrator.task_runner.os.getpgid", side_effect=fake_getpgid):
        result = run_session(tmp_path, ["claude", "-p"], "my-task", turn_limit=15)

    assert result.outcome == "stuck"
    assert any(sig == signal.SIGTERM for _, sig in pgid_calls)


def test_run_session_sets_session_type_env(tmp_path):
    captured_env: dict = {}
    mock_proc = _MockProcess([], returncode=0)

    def fake_popen(cmd, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        return mock_proc

    with patch("orchestrator.task_runner.subprocess.Popen", side_effect=fake_popen):
        run_session(tmp_path, ["claude", "-p"], "my-task", turn_limit=15, session_type="explore")

    assert captured_env.get("AUTOCLAUDE_SESSION_TYPE") == "explore"


def test_run_session_uses_start_new_session(tmp_path):
    """Verify start_new_session=True is passed to Popen."""
    mock_proc = _MockProcess([], returncode=0)
    popen_kwargs: dict = {}

    def fake_popen(cmd, **kwargs):
        popen_kwargs.update(kwargs)
        return mock_proc

    with patch("orchestrator.task_runner.subprocess.Popen", side_effect=fake_popen):
        run_session(tmp_path, ["claude", "-p"], "my-task", turn_limit=15)

    assert popen_kwargs.get("start_new_session") is True


# ---------------------------------------------------------------------------
# run_primary_session
# ---------------------------------------------------------------------------

def test_run_primary_session_dry_run(task_repo):
    with patch("orchestrator.task_runner.gm.create_branch"):
        result = run_primary_session(
            repo_path=task_repo,
            task_slug="my-task",
            branch_name="feature/my-task",
            budget_usd=5.0,
            init_prompt="init",
            turn_limit=15,
            dry_run=True,
        )
    assert result.outcome == "pass"
    assert result.note == "dry run"
    assert (task_repo / "tasks" / "done" / "my-task.md").exists()


def test_run_primary_session_pass(task_repo):
    mock_result = SessionResult(outcome="pass", turn_count=5, exit_code=0, note="Session completed")
    with patch("orchestrator.task_runner.gm.create_branch"), \
         patch("orchestrator.task_runner.gm.push_branch"), \
         patch("orchestrator.task_runner.gm.append_devlog"), \
         patch("orchestrator.task_runner.run_session", return_value=mock_result):
        result = run_primary_session(
            repo_path=task_repo,
            task_slug="my-task",
            branch_name="feature/my-task",
            budget_usd=5.0,
            init_prompt="init",
            turn_limit=15,
            dry_run=False,
        )
    assert result.outcome == "pass"
    # Task file should still be in active/ (scheduler handles post-pass cleanup)
    assert (task_repo / "tasks" / "active" / "my-task.md").exists()


def test_run_primary_session_creates_branch(task_repo):
    branch_created = []
    mock_result = SessionResult(outcome="pass", turn_count=1, exit_code=0, note="done")
    with patch("orchestrator.task_runner.gm.create_branch", side_effect=lambda p, b: branch_created.append(b)), \
         patch("orchestrator.task_runner.gm.append_devlog"), \
         patch("orchestrator.task_runner.run_session", return_value=mock_result):
        run_primary_session(
            repo_path=task_repo,
            task_slug="my-task",
            branch_name="feature/my-task",
            budget_usd=5.0,
            init_prompt="",
            turn_limit=15,
            dry_run=False,
        )
    assert branch_created == ["feature/my-task"]


# ---------------------------------------------------------------------------
# run_explore_session
# ---------------------------------------------------------------------------

def test_run_explore_session_captures_guide(task_repo):
    mock_result = SessionResult(outcome="stuck", turn_count=10, exit_code=-15, note="limit")

    # Pre-create guide file (simulating what explore session writes)
    guide_path = task_repo / "tasks" / "active" / "my-task.guide.md"
    guide_path.parent.mkdir(parents=True, exist_ok=True)
    guide_path.write_text("Root cause: missing null check")

    with patch("orchestrator.task_runner.gm.checkout_branch"), \
         patch("orchestrator.task_runner.gm.create_branch"), \
         patch("orchestrator.task_runner.gm.delete_branch"), \
         patch("orchestrator.task_runner.gm.append_devlog"), \
         patch("orchestrator.task_runner.run_session", return_value=mock_result):
        result, guide = run_explore_session(
            repo_path=task_repo,
            task_slug="my-task",
            feature_branch="feature/my-task",
            sandbox_branch="sandbox/my-task-1",
            budget_usd=2.0,
            explore_prompt="explore",
            turn_limit=15,
        )

    assert guide == "Root cause: missing null check"
    assert not guide_path.exists()  # cleaned up


def test_run_explore_session_no_guide_returns_none(task_repo):
    mock_result = SessionResult(outcome="stuck", turn_count=10, exit_code=-15, note="limit")
    with patch("orchestrator.task_runner.gm.checkout_branch"), \
         patch("orchestrator.task_runner.gm.create_branch"), \
         patch("orchestrator.task_runner.gm.delete_branch"), \
         patch("orchestrator.task_runner.gm.append_devlog"), \
         patch("orchestrator.task_runner.run_session", return_value=mock_result):
        _, guide = run_explore_session(
            repo_path=task_repo,
            task_slug="my-task",
            feature_branch="feature/my-task",
            sandbox_branch="sandbox/my-task-1",
            budget_usd=2.0,
            explore_prompt="explore",
            turn_limit=15,
        )
    assert guide is None


# ---------------------------------------------------------------------------
# run_fix_session
# ---------------------------------------------------------------------------

def test_run_fix_session_injects_guide(task_repo):
    invocation_used: list[list[str]] = []
    mock_result = SessionResult(outcome="pass", turn_count=3, exit_code=0, note="done")

    def fake_run_session(repo_path, invocation, slug, turn_limit, session_type="primary"):
        invocation_used.append(invocation)
        return mock_result

    with patch("orchestrator.task_runner.gm.checkout_branch"), \
         patch("orchestrator.task_runner.gm.append_devlog"), \
         patch("orchestrator.task_runner.run_session", side_effect=fake_run_session):
        run_fix_session(
            repo_path=task_repo,
            task_slug="my-task",
            feature_branch="feature/my-task",
            budget_usd=5.0,
            base_prompt="base",
            explore_guide="Root cause: null check",
            turn_limit=15,
        )

    prompt_idx = invocation_used[0].index("--append-system-prompt") + 1
    assert "Exploration Guide" in invocation_used[0][prompt_idx]
    assert "null check" in invocation_used[0][prompt_idx]


def test_run_fix_session_no_guide(task_repo):
    invocation_used: list[list[str]] = []
    mock_result = SessionResult(outcome="pass", turn_count=3, exit_code=0, note="done")

    def fake_run_session(repo_path, invocation, slug, turn_limit, session_type="primary"):
        invocation_used.append(invocation)
        return mock_result

    with patch("orchestrator.task_runner.gm.checkout_branch"), \
         patch("orchestrator.task_runner.gm.append_devlog"), \
         patch("orchestrator.task_runner.run_session", side_effect=fake_run_session):
        run_fix_session(
            repo_path=task_repo,
            task_slug="my-task",
            feature_branch="feature/my-task",
            budget_usd=5.0,
            base_prompt="base",
            explore_guide=None,
            turn_limit=15,
        )

    # No guide → no --append-system-prompt (base_prompt is empty string would skip it)
    # base_prompt="base" is non-empty so --append-system-prompt IS present but without guide
    prompt_idx = invocation_used[0].index("--append-system-prompt") + 1
    assert "Exploration Guide" not in invocation_used[0][prompt_idx]
