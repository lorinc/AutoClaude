import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from orchestrator.readiness_check import (
    check_task_spec,
    check_branch_clean,
    check_no_duplicate_pr,
    check_ci_green,
    run,
    REQUIRED_SECTIONS,
)

VALID_SPEC = """\
# Task: example-task

## Goal
Do the thing.

## Context and constraints
Use the existing library.

## My concerns
Performance on large inputs.

## Acceptance criteria
- Unit tests pass
- Integration test: endpoint returns 200
"""


# --- check_task_spec ---

def test_task_spec_pass(tmp_path):
    spec = tmp_path / "task.md"
    spec.write_text(VALID_SPEC)
    result = check_task_spec(spec)
    assert result.passed
    assert result.reason is None


@pytest.mark.parametrize("drop", sorted(REQUIRED_SECTIONS))
def test_task_spec_missing_section(tmp_path, drop):
    content = "\n".join(line for line in VALID_SPEC.splitlines() if line.strip() != drop)
    spec = tmp_path / "task.md"
    spec.write_text(content)
    result = check_task_spec(spec)
    assert not result.passed
    assert drop in result.reason


def test_task_spec_file_not_found(tmp_path):
    result = check_task_spec(tmp_path / "nonexistent.md")
    assert not result.passed
    assert "not found" in result.reason


def test_task_spec_section_case_sensitive(tmp_path):
    spec = tmp_path / "task.md"
    spec.write_text(VALID_SPEC.replace("## Goal", "## goal"))
    result = check_task_spec(spec)
    assert not result.passed


# --- check_branch_clean ---

@pytest.fixture
def git_repo(tmp_path):
    """Minimal git repo with one commit on main."""
    cfg = {"cwd": tmp_path, "check": True, "capture_output": True}
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], **cfg)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    (tmp_path / "README.md").write_text("init")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], check=True)
    return tmp_path


def test_branch_clean_pass(git_repo):
    result = check_branch_clean(git_repo)
    assert result.passed


def test_branch_clean_uncommitted_changes(git_repo):
    (git_repo / "dirty.txt").write_text("uncommitted")
    result = check_branch_clean(git_repo)
    assert not result.passed
    assert "Uncommitted changes" in result.reason


def test_branch_clean_unmerged_feature_branch(git_repo):
    subprocess.run(["git", "-C", str(git_repo), "checkout", "-b", "feature/new-thing"], check=True)
    (git_repo / "new.txt").write_text("new")
    subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-m", "wip"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "checkout", "main"], check=True)
    result = check_branch_clean(git_repo)
    assert not result.passed
    assert "feature/new-thing" in result.reason


def test_branch_clean_non_agent_branch_ignored(git_repo):
    """Branches not matching agent prefixes must not block."""
    subprocess.run(["git", "-C", str(git_repo), "checkout", "-b", "release/v1"], check=True)
    (git_repo / "rel.txt").write_text("release")
    subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-m", "release"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "checkout", "main"], check=True)
    result = check_branch_clean(git_repo)
    assert result.passed


# --- check_no_duplicate_pr ---

def _mock_gh(stdout: str, returncode: int = 0):
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = ""
    return mock


def test_no_duplicate_pr_pass():
    with patch("orchestrator.readiness_check.subprocess.run", return_value=_mock_gh("[]")):
        result = check_no_duplicate_pr("https://github.com/user/repo", "my-task")
    assert result.passed


def test_no_duplicate_pr_found():
    with patch("orchestrator.readiness_check.subprocess.run",
               return_value=_mock_gh('[{"number": 42}]')):
        result = check_no_duplicate_pr("https://github.com/user/repo", "my-task")
    assert not result.passed
    assert "#42" in result.reason


def test_no_duplicate_pr_gh_failure():
    mock = _mock_gh("", returncode=1)
    mock.stderr = "gh: not authenticated"
    with patch("orchestrator.readiness_check.subprocess.run", return_value=mock):
        result = check_no_duplicate_pr("https://github.com/user/repo", "my-task")
    assert not result.passed
    assert "gh pr list failed" in result.reason


# --- check_ci_green (stub) ---

def test_ci_green_always_passes():
    result = check_ci_green("https://github.com/user/repo")
    assert result.passed
    assert "stubbed" in result.reason


# --- run (integration) ---

def test_run_all_pass(git_repo):
    spec = git_repo / "tasks" / "pending" / "example-task.md"
    spec.parent.mkdir(parents=True)
    spec.write_text(VALID_SPEC)
    subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-m", "add task spec"], check=True)

    _real_run = subprocess.run  # capture before patch

    def side_effect(cmd, **kwargs):
        if cmd[0] == "gh":
            return _mock_gh("[]")
        return _real_run(cmd, **kwargs)

    with patch("orchestrator.readiness_check.subprocess.run", side_effect=side_effect):
        result = run(spec, git_repo, "https://github.com/user/repo", "example-task")

    assert result.ready


def test_run_ready_false_on_any_failure(git_repo):
    spec = git_repo / "task.md"
    spec.write_text("# incomplete spec")  # missing all sections
    with patch("orchestrator.readiness_check.subprocess.run", return_value=_mock_gh("[]")):
        result = run(spec, git_repo, "https://github.com/user/repo", "example-task")
    assert not result.ready
    assert not result.task_spec.passed
