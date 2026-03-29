import json
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.scheduler import (
    ProjectConfig,
    Scheduler,
    find_pending_task,
    find_retry_task,
    load_registry,
)
from orchestrator.state_store import StateStore, TaskState
from orchestrator.task_runner import SessionResult


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


@pytest.fixture
def store():
    s = StateStore(":memory:")
    yield s
    s.close()


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
    assert configs[0].explore_budget_fraction == 0.3


# ---------------------------------------------------------------------------
# find_retry_task / find_pending_task (filesystem helpers)
# ---------------------------------------------------------------------------

def test_find_retry_task_none_when_empty(project_repo):
    assert find_retry_task(project_repo) is None


def test_find_retry_task_finds_active(project_repo):
    (project_repo / "tasks" / "active" / "stuck-task.md").write_text("spec")
    assert find_retry_task(project_repo) == "stuck-task"


def test_find_retry_task_oldest_first(project_repo):
    import time
    a = project_repo / "tasks" / "active" / "older-task.md"
    a.write_text("spec")
    time.sleep(0.01)
    b = project_repo / "tasks" / "active" / "newer-task.md"
    b.write_text("spec")
    assert find_retry_task(project_repo) == "older-task"


def test_find_retry_task_no_active_dir(tmp_path):
    assert find_retry_task(tmp_path) is None


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
# Helpers
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
    costs_path=Path("/dev/null"),
)

_PASS_RESULT = SessionResult(outcome="pass", turn_count=5, exit_code=0, note="done")
_STUCK_RESULT = SessionResult(outcome="stuck", turn_count=15, exit_code=-15, note="limit")
_FAIL_RESULT = SessionResult(outcome="fail", turn_count=3, exit_code=1, note="error")


def make_scheduler(store=None, **overrides):
    kwargs = {**SCHEDULER_DEFAULTS, **overrides}
    if store is not None:
        kwargs["state_store"] = store
    return Scheduler(**kwargs)


def _ready_rc():
    from orchestrator.readiness_check import CheckResult, ReadinessResult
    return ReadinessResult(
        task_spec=CheckResult(passed=True),
        branch_clean=CheckResult(passed=True),
        no_duplicate_pr=CheckResult(passed=True),
        ci_green=CheckResult(passed=True),
    )


# ---------------------------------------------------------------------------
# Scheduler._process_project — primary dispatch
# ---------------------------------------------------------------------------

def test_process_project_no_tasks(project_config, store):
    s = make_scheduler(store=store)
    with patch("orchestrator.scheduler.tr.run_primary_session") as mock_primary:
        s._process_project(project_config)
    mock_primary.assert_not_called()


def test_process_project_dispatches_pending(project_config, project_repo, store):
    (project_repo / "tasks" / "pending" / "add-endpoint.md").write_text(_GOOD_SPEC)
    s = make_scheduler(store=store)
    with patch("orchestrator.scheduler.tr.run_primary_session", return_value=_PASS_RESULT), \
         patch("orchestrator.scheduler.rc.run", return_value=_ready_rc()), \
         patch("orchestrator.scheduler.cg.log_cost"):
        s._process_project(project_config)

    task = store.get_task("test-project", "add-endpoint")
    assert task is not None
    assert task.state == TaskState.PASSED


def test_process_project_dispatches_with_model(project_config, project_repo, store):
    """Model parsed from task spec is forwarded to run_primary_session."""
    spec = project_repo / "tasks" / "pending" / "chore-bump-deps.md"
    spec.write_text(_GOOD_SPEC)
    s = make_scheduler(store=store)
    captured_model = []

    def fake_primary(repo_path, task_slug, branch_name, budget_usd, init_prompt,
                     turn_limit, dry_run, model="claude-sonnet-4-6"):
        captured_model.append(model)
        return _PASS_RESULT

    with patch("orchestrator.scheduler.tr.run_primary_session", side_effect=fake_primary), \
         patch("orchestrator.scheduler.rc.run", return_value=_ready_rc()), \
         patch("orchestrator.scheduler.cg.log_cost"):
        s._process_project(project_config)

    assert captured_model == ["claude-haiku-4-5-20251001"]  # chore → haiku


def test_process_project_skips_init_prompt_for_scaffolded(project_config, project_repo, store):
    (project_repo / "CLAUDE.md").write_text("# context")
    (project_repo / "tasks" / "pending" / "add-endpoint.md").write_text(_GOOD_SPEC)
    s = make_scheduler(store=store, init_prompt="big init prompt")
    captured_prompt = []

    def fake_primary(repo_path, task_slug, branch_name, budget_usd, init_prompt,
                     turn_limit, dry_run, model="claude-sonnet-4-6"):
        captured_prompt.append(init_prompt)
        return _PASS_RESULT

    with patch("orchestrator.scheduler.tr.run_primary_session", side_effect=fake_primary), \
         patch("orchestrator.scheduler.rc.run", return_value=_ready_rc()), \
         patch("orchestrator.scheduler.cg.log_cost"):
        s._process_project(project_config)

    assert captured_prompt == [""]


def test_process_project_sends_init_prompt_when_not_scaffolded(project_config, project_repo, store):
    (project_repo / "tasks" / "pending" / "add-endpoint.md").write_text(_GOOD_SPEC)
    s = make_scheduler(store=store, init_prompt="big init prompt")
    captured_prompt = []

    def fake_primary(repo_path, task_slug, branch_name, budget_usd, init_prompt,
                     turn_limit, dry_run, model="claude-sonnet-4-6"):
        captured_prompt.append(init_prompt)
        return _PASS_RESULT

    with patch("orchestrator.scheduler.tr.run_primary_session", side_effect=fake_primary), \
         patch("orchestrator.scheduler.rc.run", return_value=_ready_rc()), \
         patch("orchestrator.scheduler.cg.log_cost"):
        s._process_project(project_config)

    assert captured_prompt == ["big init prompt"]


def test_process_project_skips_when_not_ready(project_config, project_repo, store):
    (project_repo / "tasks" / "pending" / "add-endpoint.md").write_text(_GOOD_SPEC)
    s = make_scheduler(store=store)
    from orchestrator.readiness_check import CheckResult, ReadinessResult
    not_ready = ReadinessResult(
        task_spec=CheckResult(passed=False, reason="Missing sections"),
        branch_clean=CheckResult(passed=True),
        no_duplicate_pr=CheckResult(passed=True),
        ci_green=CheckResult(passed=True),
    )
    with patch("orchestrator.scheduler.tr.run_primary_session") as mock_primary, \
         patch("orchestrator.scheduler.rc.run", return_value=not_ready):
        s._process_project(project_config)
    mock_primary.assert_not_called()


def test_process_project_ad_hoc_blocking(project_config, project_repo, store):
    (project_repo / "tasks" / "pending" / "new-feature.md").write_text(_GOOD_SPEC)
    s = make_scheduler(store=store)
    from orchestrator.readiness_check import CheckResult, ReadinessResult
    blocked = ReadinessResult(
        task_spec=CheckResult(passed=True),
        branch_clean=CheckResult(passed=False, reason="Unmerged agent branches: ['feature/old']"),
        no_duplicate_pr=CheckResult(passed=True),
        ci_green=CheckResult(passed=True),
    )
    with patch("orchestrator.scheduler.tr.run_primary_session") as mock_primary, \
         patch("orchestrator.scheduler.rc.run", return_value=blocked):
        s._process_project(project_config)
    mock_primary.assert_not_called()


# ---------------------------------------------------------------------------
# Scheduler._process_project — EXPLORE dispatch
# ---------------------------------------------------------------------------

def test_process_project_explore_takes_priority_over_pending(project_config, project_repo, store):
    """EXPLORE task wins over PENDING task."""
    # Create a PENDING task (from filesystem)
    (project_repo / "tasks" / "pending" / "new-task.md").write_text(_GOOD_SPEC)
    # Create an EXPLORE task directly in the DB
    spec = project_repo / "tasks" / "active" / "stuck-task.md"
    spec.write_text(_GOOD_SPEC)

    s = make_scheduler(store=store)
    dispatched_as = []

    with patch("orchestrator.scheduler.tr.run_explore_session",
               return_value=(_STUCK_RESULT, "guide content")) as mock_explore, \
         patch("orchestrator.scheduler.tr.run_primary_session") as mock_primary, \
         patch("orchestrator.scheduler.cg.log_cost"):
        s._process_project(project_config)

    mock_explore.assert_called_once()
    mock_primary.assert_not_called()
    # After explore with guide: task transitions to FIXING
    task = store.get_task("test-project", "stuck-task")
    assert task.state == TaskState.FIXING
    assert task.explore_guide == "guide content"


def test_process_project_explore_skips_readiness_check(project_config, project_repo, store):
    """Readiness check is NOT run for EXPLORE tasks."""
    (project_repo / "tasks" / "active" / "stuck-task.md").write_text(_GOOD_SPEC)
    s = make_scheduler(store=store)
    with patch("orchestrator.scheduler.tr.run_explore_session",
               return_value=(_STUCK_RESULT, None)), \
         patch("orchestrator.scheduler.rc.run") as mock_rc, \
         patch("orchestrator.scheduler.cg.log_cost"):
        s._process_project(project_config)
    mock_rc.assert_not_called()


def test_process_project_explore_no_guide_escalates(project_config, project_repo, store):
    (project_repo / "tasks" / "active" / "stuck-task.md").write_text(_GOOD_SPEC)
    notified = []
    s = make_scheduler(store=store, notify_stuck=lambda *a: notified.append(a))
    with patch("orchestrator.scheduler.tr.run_explore_session",
               return_value=(_STUCK_RESULT, None)), \
         patch("orchestrator.scheduler.cg.log_cost"):
        s._process_project(project_config)

    task = store.get_task("test-project", "stuck-task")
    assert task.state == TaskState.ESCALATED
    assert len(notified) == 1


# ---------------------------------------------------------------------------
# Scheduler._process_project — FIXING dispatch
# ---------------------------------------------------------------------------

def test_process_project_fix_pass_marks_passed(project_config, project_repo, store):
    """Fix session pass → PASSED state."""
    spec_path = project_repo / "tasks" / "active" / "stuck-task.md"
    spec_path.write_text(_GOOD_SPEC)
    # Sync into DB and move to FIXING state
    s = make_scheduler(store=store, dry_run=True)
    s._sync_fs_to_db(project_config)
    store.transition("test-project", "stuck-task", TaskState.EXPLORE, TaskState.FIXING,
                     explore_guide="Root cause: X")

    with patch("orchestrator.scheduler.tr.run_fix_session", return_value=_PASS_RESULT), \
         patch("orchestrator.scheduler.cg.log_cost"):
        s._process_project(project_config)

    task = store.get_task("test-project", "stuck-task")
    assert task.state == TaskState.PASSED


def test_process_project_fix_stuck_retries_via_explore(project_config, project_repo, store):
    """Fix session stuck (retry_n < max) → EXPLORE again."""
    spec_path = project_repo / "tasks" / "active" / "stuck-task.md"
    spec_path.write_text(_GOOD_SPEC)
    s = make_scheduler(store=store, dry_run=True, max_retries=3)
    s._sync_fs_to_db(project_config)
    # Set retry_n=1 and FIXING state
    store.transition("test-project", "stuck-task", TaskState.EXPLORE, TaskState.FIXING,
                     explore_guide="guide", retry_n=1)
    # Override retry_n to 1
    store._conn.execute("UPDATE tasks SET retry_n=1 WHERE slug='stuck-task'")
    store._conn.commit()

    with patch("orchestrator.scheduler.tr.run_fix_session", return_value=_STUCK_RESULT), \
         patch("orchestrator.scheduler.cg.log_cost"):
        s._process_project(project_config)

    task = store.get_task("test-project", "stuck-task")
    assert task.state == TaskState.EXPLORE  # back to explore
    assert task.retry_n == 2
    assert task.explore_guide is None  # reset for fresh exploration


def test_process_project_fix_stuck_max_retries_escalates(project_config, project_repo, store):
    """Fix session stuck at max_retries → ESCALATED."""
    spec_path = project_repo / "tasks" / "active" / "stuck-task.md"
    spec_path.write_text(_GOOD_SPEC)
    notified = []
    s = make_scheduler(store=store, dry_run=True, max_retries=3,
                       notify_stuck=lambda *a: notified.append(a))
    s._sync_fs_to_db(project_config)
    store.transition("test-project", "stuck-task", TaskState.EXPLORE, TaskState.FIXING,
                     explore_guide="guide")
    # Set retry_n to max_retries - 1 so next attempt hits limit
    store._conn.execute("UPDATE tasks SET retry_n=2 WHERE slug='stuck-task'")
    store._conn.commit()

    with patch("orchestrator.scheduler.tr.run_fix_session", return_value=_STUCK_RESULT), \
         patch("orchestrator.scheduler.cg.log_cost"):
        s._process_project(project_config)

    task = store.get_task("test-project", "stuck-task")
    assert task.state == TaskState.ESCALATED
    assert len(notified) == 1


# ---------------------------------------------------------------------------
# Scheduler.run — loop behaviour
# ---------------------------------------------------------------------------

def test_scheduler_run_empty_registry_sleeps_and_exits(tmp_path):
    reg = tmp_path / "project_registry.json"
    reg.write_text("[]")
    s = Scheduler(registry_path=reg, init_prompt="init", turn_limit=15,
                  max_retries=3, poll_interval=999, dry_run=True)
    call_count = 0

    def fake_sleep(seconds):
        nonlocal call_count
        call_count += 1
        s._shutdown = True

    with patch("orchestrator.scheduler.time.sleep", fake_sleep):
        s.run()
    assert call_count == 1


def test_scheduler_run_sets_sigterm_handler(tmp_path):
    reg = tmp_path / "project_registry.json"
    reg.write_text("[]")
    s = Scheduler(registry_path=reg, init_prompt="init", turn_limit=15,
                  max_retries=3, poll_interval=0, dry_run=True)
    s._shutdown = True
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
    s = Scheduler(registry_path=reg, init_prompt="init", turn_limit=15,
                  max_retries=3, poll_interval=999, dry_run=True)
    processed = []

    def fake_process(project):
        processed.append(project.name)
        s._shutdown = True

    s._process_project = fake_process
    with patch("orchestrator.scheduler.time.sleep"):
        s.run()
    assert "proj-a" in processed


def test_scheduler_shutdown_mid_loop(tmp_path):
    reg = tmp_path / "project_registry.json"
    reg.write_text(json.dumps([
        {"name": "proj-a", "repo_path": str(tmp_path / "a"), "github_url": "https://github.com/u/a"},
        {"name": "proj-b", "repo_path": str(tmp_path / "b"), "github_url": "https://github.com/u/b"},
    ]))
    s = Scheduler(registry_path=reg, init_prompt="init", turn_limit=15,
                  max_retries=3, poll_interval=999, dry_run=True)
    processed = []

    def fake_process(project):
        processed.append(project.name)
        s._shutdown = True

    s._process_project = fake_process
    with patch("orchestrator.scheduler.time.sleep"):
        s.run()
    assert processed == ["proj-a"]
