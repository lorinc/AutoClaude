import json
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from orchestrator.scheduler import (
    ProjectConfig,
    Scheduler,
    find_pending_task,
    find_retry_task,
    load_registry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def registry_path(tmp_path):
    path = tmp_path / "project_registry.json"
    path.write_text("[]")
    return path


@pytest.fixture
def project_repo(tmp_path):
    """Minimal git repo with task directories."""
    cfg = {"check": True, "capture_output": True}
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], **cfg)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], **cfg)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], **cfg)
    (tmp_path / "devlog.md").write_text("")
    for d in ("pending", "active", "done"):
        (tmp_path / "tasks" / d).mkdir(parents=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], **cfg)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], **cfg)
    return tmp_path


@pytest.fixture
def project_config(project_repo):
    return ProjectConfig(
        name="test-project",
        repo_path=project_repo,
        github_url="https://github.com/user/test-project",
        budget_limit_usd=5.0,
    )


# ---------------------------------------------------------------------------
# load_registry
# ---------------------------------------------------------------------------

def test_load_registry_empty(registry_path):
    assert load_registry(registry_path) == []


def test_load_registry_parses_entries(tmp_path):
    path = tmp_path / "project_registry.json"
    path.write_text(json.dumps([
        {
            "name": "proj-a",
            "repo_path": "/home/agent/repos/proj-a",
            "github_url": "https://github.com/user/proj-a",
            "budget_limit_usd": 8.0,
            "extra_field_ignored": True,
        }
    ]))
    configs = load_registry(path)
    assert len(configs) == 1
    assert configs[0].name == "proj-a"
    assert configs[0].repo_path == Path("/home/agent/repos/proj-a")
    assert configs[0].budget_limit_usd == 8.0


def test_load_registry_defaults(tmp_path):
    path = tmp_path / "project_registry.json"
    path.write_text(json.dumps([{
        "name": "proj-b",
        "repo_path": "/tmp/proj-b",
        "github_url": "https://github.com/user/proj-b",
    }]))
    configs = load_registry(path)
    assert configs[0].budget_limit_usd == 10.0
    assert configs[0].python_version == "3.12"


# ---------------------------------------------------------------------------
# find_retry_task
# ---------------------------------------------------------------------------

def test_find_retry_task_none_when_empty(project_repo):
    assert find_retry_task(project_repo) is None


def test_find_retry_task_finds_active(project_repo):
    (project_repo / "tasks" / "active" / "stuck-task.md").write_text("spec")
    assert find_retry_task(project_repo) == "stuck-task"


def test_find_retry_task_oldest_first(project_repo, tmp_path):
    import time
    a = project_repo / "tasks" / "active" / "older-task.md"
    a.write_text("spec")
    time.sleep(0.01)
    b = project_repo / "tasks" / "active" / "newer-task.md"
    b.write_text("spec")
    assert find_retry_task(project_repo) == "older-task"


def test_find_retry_task_no_active_dir(tmp_path):
    assert find_retry_task(tmp_path) is None


# ---------------------------------------------------------------------------
# find_pending_task
# ---------------------------------------------------------------------------

def test_find_pending_task_none_when_empty(project_repo):
    assert find_pending_task(project_repo) is None


def test_find_pending_task_finds_pending(project_repo):
    (project_repo / "tasks" / "pending" / "my-task.md").write_text("spec")
    assert find_pending_task(project_repo) == "my-task"


def test_find_pending_task_oldest_first(project_repo):
    import time
    (project_repo / "tasks" / "pending" / "old-task.md").write_text("spec")
    time.sleep(0.01)
    (project_repo / "tasks" / "pending" / "new-task.md").write_text("spec")
    assert find_pending_task(project_repo) == "old-task"


def test_find_pending_task_no_pending_dir(tmp_path):
    assert find_pending_task(tmp_path) is None


# ---------------------------------------------------------------------------
# Scheduler._process_project
# ---------------------------------------------------------------------------

_GOOD_SPEC = (
    "# Task\n"
    "## Goal\ndo stuff\n"
    "## Context and constraints\nnone\n"
    "## My concerns\nnone\n"
    "## Acceptance criteria\nit works\n"
)

SCHEDULER_DEFAULTS = dict(
    registry_path=Path("/dev/null"),
    init_prompt="init",
    turn_limit=15,
    max_retries=3,
    poll_interval=60,
    dry_run=True,
)


def make_scheduler(**overrides):
    kwargs = {**SCHEDULER_DEFAULTS, **overrides}
    return Scheduler(**kwargs)


def test_process_project_no_tasks(project_config):
    s = make_scheduler()
    mock_run_task = MagicMock()
    with patch("orchestrator.scheduler.tr.run_task", mock_run_task):
        s._process_project(project_config)
    mock_run_task.assert_not_called()


def test_process_project_dispatches_pending(project_config, project_repo):
    (project_repo / "tasks" / "pending" / "add-endpoint.md").write_text(_GOOD_SPEC)
    s = make_scheduler()
    mock_run_task = MagicMock()
    with patch("orchestrator.scheduler.tr.run_task", mock_run_task), \
         patch("orchestrator.scheduler.rc.run") as mock_rc:
        from orchestrator.readiness_check import CheckResult, ReadinessResult
        mock_rc.return_value = ReadinessResult(
            task_spec=CheckResult(passed=True),
            branch_clean=CheckResult(passed=True),
            no_duplicate_pr=CheckResult(passed=True),
            ci_green=CheckResult(passed=True),
        )
        s._process_project(project_config)

    mock_run_task.assert_called_once()
    kwargs = mock_run_task.call_args.kwargs
    assert kwargs["task_slug"] == "add-endpoint"
    assert kwargs["is_retry"] is False


def test_process_project_retry_takes_priority(project_config, project_repo):
    # Both active and pending tasks exist — retry should win
    (project_repo / "tasks" / "active" / "stuck-task.md").write_text("spec")
    (project_repo / "tasks" / "pending" / "new-task.md").write_text(_GOOD_SPEC)
    s = make_scheduler()
    mock_run_task = MagicMock()
    with patch("orchestrator.scheduler.tr.run_task", mock_run_task):
        s._process_project(project_config)

    mock_run_task.assert_called_once()
    kwargs = mock_run_task.call_args.kwargs
    assert kwargs["task_slug"] == "stuck-task"
    assert kwargs["is_retry"] is True


def test_process_project_retry_skips_readiness_check(project_config, project_repo):
    (project_repo / "tasks" / "active" / "stuck-task.md").write_text("spec")
    s = make_scheduler()
    with patch("orchestrator.scheduler.tr.run_task"), \
         patch("orchestrator.scheduler.rc.run") as mock_rc:
        s._process_project(project_config)
    mock_rc.assert_not_called()


def test_process_project_skips_when_not_ready(project_config, project_repo):
    (project_repo / "tasks" / "pending" / "add-endpoint.md").write_text(_GOOD_SPEC)
    s = make_scheduler()
    mock_run_task = MagicMock()
    with patch("orchestrator.scheduler.tr.run_task", mock_run_task), \
         patch("orchestrator.scheduler.rc.run") as mock_rc:
        from orchestrator.readiness_check import CheckResult, ReadinessResult
        mock_rc.return_value = ReadinessResult(
            task_spec=CheckResult(passed=False, reason="Missing sections: ['## Goal']"),
            branch_clean=CheckResult(passed=True),
            no_duplicate_pr=CheckResult(passed=True),
            ci_green=CheckResult(passed=True),
        )
        s._process_project(project_config)

    mock_run_task.assert_not_called()


def test_process_project_ad_hoc_blocking(project_config, project_repo):
    """Unmerged branch → pending task blocked, no dispatch, no error."""
    (project_repo / "tasks" / "pending" / "new-feature.md").write_text(_GOOD_SPEC)
    s = make_scheduler()
    mock_run_task = MagicMock()
    with patch("orchestrator.scheduler.tr.run_task", mock_run_task), \
         patch("orchestrator.scheduler.rc.run") as mock_rc:
        from orchestrator.readiness_check import CheckResult, ReadinessResult
        mock_rc.return_value = ReadinessResult(
            task_spec=CheckResult(passed=True),
            branch_clean=CheckResult(
                passed=False,
                reason="Unmerged agent branches: ['feature/old-task']",
            ),
            no_duplicate_pr=CheckResult(passed=True),
            ci_green=CheckResult(passed=True),
        )
        s._process_project(project_config)

    mock_run_task.assert_not_called()


# ---------------------------------------------------------------------------
# Scheduler.run — graceful SIGTERM
# ---------------------------------------------------------------------------

def test_scheduler_run_empty_registry_sleeps_and_exits(tmp_path):
    reg = tmp_path / "project_registry.json"
    reg.write_text("[]")
    s = Scheduler(
        registry_path=reg,
        init_prompt="init",
        turn_limit=15,
        max_retries=3,
        poll_interval=999,
        dry_run=True,
    )
    call_count = 0

    def fake_sleep(seconds):
        nonlocal call_count
        call_count += 1
        s._shutdown = True  # signal shutdown after first sleep

    with patch("orchestrator.scheduler.time.sleep", fake_sleep):
        s.run()

    assert call_count == 1


def test_scheduler_run_sets_sigterm_handler(tmp_path):
    reg = tmp_path / "project_registry.json"
    reg.write_text("[]")
    s = Scheduler(
        registry_path=reg,
        init_prompt="init",
        turn_limit=15,
        max_retries=3,
        poll_interval=0,
        dry_run=True,
    )
    s._shutdown = True  # exit immediately after first iteration
    registered = {}

    original_signal = signal.signal

    def capture_signal(signum, handler):
        registered[signum] = handler
        return original_signal(signum, handler)

    with patch("orchestrator.scheduler.signal.signal", side_effect=capture_signal), \
         patch("orchestrator.scheduler.time.sleep"):
        s.run()

    assert signal.SIGTERM in registered


def test_scheduler_run_processes_projects(tmp_path):
    reg = tmp_path / "project_registry.json"
    reg.write_text(json.dumps([{
        "name": "proj-a",
        "repo_path": str(tmp_path / "proj-a"),
        "github_url": "https://github.com/user/proj-a",
    }]))
    s = Scheduler(
        registry_path=reg,
        init_prompt="init",
        turn_limit=15,
        max_retries=3,
        poll_interval=999,
        dry_run=True,
    )
    processed = []

    def fake_process(project):
        processed.append(project.name)
        s._shutdown = True  # exit after first project

    s._process_project = fake_process

    with patch("orchestrator.scheduler.time.sleep"):
        s.run()

    assert "proj-a" in processed


def test_scheduler_shutdown_mid_loop(tmp_path):
    """SIGTERM between projects stops the loop without processing further projects."""
    reg = tmp_path / "project_registry.json"
    reg.write_text(json.dumps([
        {"name": "proj-a", "repo_path": str(tmp_path / "a"), "github_url": "https://github.com/u/a"},
        {"name": "proj-b", "repo_path": str(tmp_path / "b"), "github_url": "https://github.com/u/b"},
    ]))
    s = Scheduler(
        registry_path=reg,
        init_prompt="init",
        turn_limit=15,
        max_retries=3,
        poll_interval=999,
        dry_run=True,
    )
    processed = []

    def fake_process(project):
        processed.append(project.name)
        s._shutdown = True  # simulate SIGTERM after first project

    s._process_project = fake_process

    with patch("orchestrator.scheduler.time.sleep"):
        s.run()

    assert processed == ["proj-a"]  # proj-b never reached
