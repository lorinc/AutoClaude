"""
Tests for git_manager. All subprocess.run calls are monkeypatched.
Real git repos are used only for integration-style devlog tests.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import orchestrator.git_manager as gm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(stdout="", returncode=0, stderr=""):
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.stdout = stdout
    r.returncode = returncode
    r.stderr = stderr
    return r


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


# ---------------------------------------------------------------------------
# create_branch / checkout_branch / current_branch
# ---------------------------------------------------------------------------

def test_create_branch_calls_git(tmp_path):
    with patch("orchestrator.git_manager.subprocess.run", return_value=_make_result()) as mock_run:
        gm.create_branch(tmp_path, "feature/my-task")
    mock_run.assert_called_once_with(
        ["git", "-C", str(tmp_path), "checkout", "-b", "feature/my-task"],
        check=True, capture_output=True, text=True,
    )


def test_checkout_branch_calls_git(tmp_path):
    with patch("orchestrator.git_manager.subprocess.run", return_value=_make_result()) as mock_run:
        gm.checkout_branch(tmp_path, "feature/my-task")
    mock_run.assert_called_once_with(
        ["git", "-C", str(tmp_path), "checkout", "feature/my-task"],
        check=True, capture_output=True, text=True,
    )


def test_create_branch_raises_on_failure(tmp_path):
    err = subprocess.CalledProcessError(128, ["git"])
    err.stderr = "branch already exists"
    with patch("orchestrator.git_manager.subprocess.run", side_effect=err):
        with pytest.raises(subprocess.CalledProcessError):
            gm.create_branch(tmp_path, "feature/my-task")


def test_current_branch(tmp_path):
    with patch("orchestrator.git_manager.subprocess.run", return_value=_make_result("main\n")):
        assert gm.current_branch(tmp_path) == "main"


# ---------------------------------------------------------------------------
# delete_branch
# ---------------------------------------------------------------------------

def test_delete_branch_local_only(tmp_path):
    calls = []
    with patch("orchestrator.git_manager.subprocess.run",
               side_effect=lambda cmd, **kw: calls.append(cmd) or _make_result()):
        gm.delete_branch(tmp_path, "feature/my-task", remote=False)
    assert len(calls) == 1
    assert "branch" in calls[0] and "-D" in calls[0]


def test_delete_branch_with_remote(tmp_path):
    calls = []
    with patch("orchestrator.git_manager.subprocess.run",
               side_effect=lambda cmd, **kw: calls.append(cmd) or _make_result()):
        gm.delete_branch(tmp_path, "feature/my-task", remote=True)
    assert len(calls) == 2
    assert any("push" in c for c in calls)


# ---------------------------------------------------------------------------
# push_branch — non-fatal on failure
# ---------------------------------------------------------------------------

def test_push_branch_success(tmp_path):
    with patch("orchestrator.git_manager.subprocess.run", return_value=_make_result(returncode=0)):
        gm.push_branch(tmp_path, "feature/my-task")  # should not raise


def test_push_branch_failure_is_non_fatal(tmp_path):
    with patch("orchestrator.git_manager.subprocess.run",
               return_value=_make_result(returncode=1, stderr="remote rejected")):
        gm.push_branch(tmp_path, "feature/my-task")  # should not raise


# ---------------------------------------------------------------------------
# commit_wip
# ---------------------------------------------------------------------------

def test_commit_wip_no_changes(tmp_path):
    calls = []
    with patch("orchestrator.git_manager.subprocess.run",
               side_effect=lambda cmd, **kw: calls.append(cmd) or _make_result(stdout="")):
        gm.commit_wip(tmp_path, "my-task", 1)
    # Only the status check should run — no commit
    assert all("commit" not in c for c in calls)


def test_commit_wip_dirty_tree(tmp_path):
    calls = []

    def _run(cmd, **kw):
        calls.append(cmd)
        if "status" in cmd:
            return _make_result(stdout="M file.py")
        return _make_result()

    with patch("orchestrator.git_manager.subprocess.run", side_effect=_run):
        gm.commit_wip(tmp_path, "my-task", 2)
    assert any("commit" in c for c in calls)
    commit_cmd = next(c for c in calls if "commit" in c)
    assert "WIP: my-task stuck attempt 2" in commit_cmd


# ---------------------------------------------------------------------------
# append_devlog
# ---------------------------------------------------------------------------

def test_append_devlog(tmp_path):
    devlog = tmp_path / "devlog.md"
    devlog.write_text("")
    gm.append_devlog(tmp_path, "my-task", "pass", "all good")
    content = devlog.read_text()
    assert "TASK:my-task" in content
    assert "OUTCOME:pass" in content
    assert "NOTE:all good" in content


def test_append_devlog_appends(tmp_path):
    devlog = tmp_path / "devlog.md"
    devlog.write_text("existing\n")
    gm.append_devlog(tmp_path, "my-task", "pass", "done")
    lines = devlog.read_text().splitlines()
    assert lines[0] == "existing"
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# get_retry_count (real git)
# ---------------------------------------------------------------------------

def test_get_retry_count_zero(git_repo):
    assert gm.get_retry_count(git_repo, "my-task") == 0


def test_get_retry_count_counts_stuck(git_repo):
    devlog = git_repo / "devlog.md"
    devlog.write_text(
        "[2026-01-01 10:00] TASK:my-task OUTCOME:stuck NOTE:attempt 1\n"
        "[2026-01-01 11:00] TASK:my-task OUTCOME:stuck NOTE:attempt 2\n"
        "[2026-01-01 12:00] TASK:other OUTCOME:stuck NOTE:other\n"
    )
    subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-m", "devlog"], check=True, capture_output=True)
    assert gm.get_retry_count(git_repo, "my-task") == 2


# ---------------------------------------------------------------------------
# promote_devlog_to_main (real git)
# ---------------------------------------------------------------------------

def test_promote_devlog_to_main(git_repo):
    subprocess.run(["git", "-C", str(git_repo), "checkout", "-b", "feature/my-task"],
                   check=True, capture_output=True)
    devlog = git_repo / "devlog.md"
    devlog.write_text("[2026-01-01 10:00] TASK:my-task OUTCOME:stuck NOTE:lessons\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-m", "devlog"], check=True, capture_output=True)

    gm.promote_devlog_to_main(git_repo, "my-task", "feature/my-task")

    # Should be back on feature branch
    branch = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert branch == "feature/my-task"

    # Lessons should be on main
    main_devlog = subprocess.run(
        ["git", "-C", str(git_repo), "show", "main:devlog.md"],
        capture_output=True, text=True,
    ).stdout
    assert "TASK:my-task" in main_devlog
    assert "lessons" in main_devlog


def test_promote_devlog_noop_when_no_diff(git_repo):
    # No changes on branch — should not raise
    gm.promote_devlog_to_main(git_repo, "my-task", "main")
