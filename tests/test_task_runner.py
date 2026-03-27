import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.task_runner import (
    SessionResult,
    _count_tool_uses,
    append_devlog,
    build_invocation,
    commit_wip,
    create_branch,
    get_retry_count,
    infer_branch_type,
    move_task,
    promote_devlog_to_main,
    run_session,
    run_task,
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
    """git_repo with tasks/ directories and a pending task."""
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
    assert "--max-budget-usd" in inv
    assert "5.0" in inv
    assert "--output-format" in inv
    assert "stream-json" in inv
    assert "--append-system-prompt" in inv
    assert "system prompt here" in inv


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
# append_devlog
# ---------------------------------------------------------------------------

def test_append_devlog(tmp_path):
    devlog = tmp_path / "devlog.md"
    devlog.write_text("")
    append_devlog(tmp_path, "my-task", "pass", "all good")
    content = devlog.read_text()
    assert "TASK:my-task" in content
    assert "OUTCOME:pass" in content
    assert "NOTE:all good" in content


def test_append_devlog_is_append_only(tmp_path):
    devlog = tmp_path / "devlog.md"
    devlog.write_text("existing line\n")
    append_devlog(tmp_path, "my-task", "pass", "done")
    lines = devlog.read_text().splitlines()
    assert lines[0] == "existing line"
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# get_retry_count
# ---------------------------------------------------------------------------

def test_get_retry_count_zero(git_repo):
    assert get_retry_count(git_repo, "my-task") == 0


def test_get_retry_count_counts_stuck(git_repo):
    devlog = git_repo / "devlog.md"
    devlog.write_text(
        "[2026-01-01 10:00] TASK:my-task OUTCOME:stuck NOTE:attempt 1\n"
        "[2026-01-01 11:00] TASK:my-task OUTCOME:stuck NOTE:attempt 2\n"
        "[2026-01-01 12:00] TASK:other-task OUTCOME:stuck NOTE:irrelevant\n"
    )
    subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-m", "update devlog"], check=True, capture_output=True)
    assert get_retry_count(git_repo, "my-task") == 2


# ---------------------------------------------------------------------------
# commit_wip
# ---------------------------------------------------------------------------

def test_commit_wip_nothing_to_commit(git_repo):
    commit_wip(git_repo, "my-task", 1)  # should not raise
    log = subprocess.run(
        ["git", "-C", str(git_repo), "log", "--oneline"],
        capture_output=True, text=True,
    ).stdout
    assert "WIP" not in log


def test_commit_wip_dirty_tree(git_repo):
    (git_repo / "new_file.py").write_text("print('wip')")
    commit_wip(git_repo, "my-task", 1)
    log = subprocess.run(
        ["git", "-C", str(git_repo), "log", "--oneline"],
        capture_output=True, text=True,
    ).stdout
    assert "WIP: my-task stuck attempt 1" in log


# ---------------------------------------------------------------------------
# promote_devlog_to_main
# ---------------------------------------------------------------------------

def test_promote_devlog_to_main(git_repo):
    # Create feature branch with a new devlog entry
    subprocess.run(["git", "-C", str(git_repo), "checkout", "-b", "feature/my-task"], check=True, capture_output=True)
    devlog = git_repo / "devlog.md"
    devlog.write_text("[2026-01-01 10:00] TASK:my-task OUTCOME:stuck NOTE:lessons here\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-m", "devlog"], check=True, capture_output=True)

    promote_devlog_to_main(git_repo, "my-task", "feature/my-task")

    # Verify we're back on feature branch
    branch = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert branch == "feature/my-task"

    # Verify entry is now on main
    main_devlog = subprocess.run(
        ["git", "-C", str(git_repo), "show", "main:devlog.md"],
        capture_output=True, text=True,
    ).stdout
    assert "TASK:my-task" in main_devlog
    assert "lessons here" in main_devlog


def test_promote_devlog_no_diff(git_repo):
    # No new entries on the branch — should be a no-op
    promote_devlog_to_main(git_repo, "my-task", "main")  # branch == main → no diff


# ---------------------------------------------------------------------------
# run_session
# ---------------------------------------------------------------------------

class _MockProcess:
    def __init__(self, lines: list[str], returncode: int = 0):
        self.stdout = iter(lines)
        self.returncode = returncode
        self.sigterm_sent = False

    def send_signal(self, sig):
        if sig == signal.SIGTERM:
            self.sigterm_sent = True

    def wait(self, timeout=None):
        pass

    def kill(self):
        pass


def test_run_session_pass(tmp_path):
    lines = ['{"type":"assistant","message":{"content":[{"type":"text","text":"done"}]}}']
    mock_proc = _MockProcess(lines, returncode=0)
    with patch("orchestrator.task_runner.subprocess.Popen", return_value=mock_proc):
        result = run_session(tmp_path, ["claude", "-p"], "my-task", turn_limit=15)
    assert result.outcome == "pass"
    assert result.exit_code == 0


def test_run_session_fail(tmp_path):
    mock_proc = _MockProcess([], returncode=1)
    with patch("orchestrator.task_runner.subprocess.Popen", return_value=mock_proc):
        result = run_session(tmp_path, ["claude", "-p"], "my-task", turn_limit=15)
    assert result.outcome == "fail"
    assert result.exit_code == 1


def test_run_session_stuck_at_turn_limit(tmp_path):
    tool_line = '{"type":"assistant","message":{"content":[{"type":"tool_use"}]}}'
    lines = [tool_line] * 20
    mock_proc = _MockProcess(lines, returncode=-15)
    with patch("orchestrator.task_runner.subprocess.Popen", return_value=mock_proc):
        result = run_session(tmp_path, ["claude", "-p"], "my-task", turn_limit=15)
    assert result.outcome == "stuck"
    assert mock_proc.sigterm_sent


# ---------------------------------------------------------------------------
# run_task (integration)
# ---------------------------------------------------------------------------

def test_run_task_dry_run(task_repo):
    result = run_task(
        repo_path=task_repo,
        task_slug="my-task",
        budget_usd=5.0,
        init_prompt="init",
        turn_limit=15,
        dry_run=True,
        notify_stuck=lambda *a: None,
    )
    assert result.outcome == "pass"
    assert result.note == "dry run"
    assert (task_repo / "tasks" / "done" / "my-task.md").exists()


def test_run_task_pass(task_repo):
    mock_result = SessionResult(outcome="pass", turn_count=5, exit_code=0, note="Session completed")
    with patch("orchestrator.task_runner.run_session", return_value=mock_result):
        result = run_task(
            repo_path=task_repo,
            task_slug="my-task",
            budget_usd=5.0,
            init_prompt="init",
            turn_limit=15,
            dry_run=False,
            notify_stuck=lambda *a: None,
        )
    assert result.outcome == "pass"
    assert (task_repo / "tasks" / "done" / "my-task.md").exists()
    devlog = (task_repo / "devlog.md").read_text()
    assert "TASK:my-task" in devlog
    assert "OUTCOME:pass" in devlog


def test_run_task_stuck_under_limit(task_repo):
    mock_result = SessionResult(outcome="stuck", turn_count=15, exit_code=-15, note="Turn limit (15) reached")
    notified = []
    with patch("orchestrator.task_runner.run_session", return_value=mock_result):
        run_task(
            repo_path=task_repo,
            task_slug="my-task",
            budget_usd=5.0,
            init_prompt="init",
            turn_limit=15,
            dry_run=False,
            notify_stuck=lambda *a: notified.append(a),
            max_retries=3,
        )
    assert not notified  # retry 1 < max 3, no alert
    assert (task_repo / "tasks" / "active" / "my-task.md").exists()  # stays for retry
    assert not (task_repo / "tasks" / "done" / "my-task.md").exists()


def test_run_task_stuck_max_retries(task_repo):
    # Seed main devlog with 2 prior stuck entries so retry_n = 3
    devlog = task_repo / "devlog.md"
    devlog.write_text(
        "[2026-01-01 10:00] TASK:my-task OUTCOME:stuck NOTE:attempt 1\n"
        "[2026-01-01 11:00] TASK:my-task OUTCOME:stuck NOTE:attempt 2\n"
    )
    subprocess.run(["git", "-C", str(task_repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(task_repo), "commit", "-m", "prior stucks"], check=True, capture_output=True)

    mock_result = SessionResult(outcome="stuck", turn_count=15, exit_code=-15, note="Turn limit (15) reached")
    notified = []
    with patch("orchestrator.task_runner.run_session", return_value=mock_result):
        run_task(
            repo_path=task_repo,
            task_slug="my-task",
            budget_usd=5.0,
            init_prompt="init",
            turn_limit=15,
            dry_run=False,
            notify_stuck=lambda slug, n, branch: notified.append((slug, n, branch)),
            max_retries=3,
        )
    assert notified[0][0] == "my-task"
    assert notified[0][1] == 3
    assert (task_repo / "tasks" / "done" / "my-task.md").exists()
