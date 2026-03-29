"""Tests for StateStore — all use an in-memory SQLite database."""

import threading

import pytest

from orchestrator.state_store import StateStore, TaskState, TERMINAL_STATES


@pytest.fixture
def store():
    s = StateStore(":memory:")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# add_task
# ---------------------------------------------------------------------------

def test_add_task_returns_true_on_insert(store):
    assert store.add_task("proj", "fix-bug", "/tasks/fix-bug.md") is True


def test_add_task_returns_false_on_duplicate(store):
    store.add_task("proj", "fix-bug", "/tasks/fix-bug.md")
    assert store.add_task("proj", "fix-bug", "/tasks/fix-bug.md") is False


def test_add_task_different_projects_same_slug(store):
    assert store.add_task("proj-a", "fix-bug", "/a/fix-bug.md") is True
    assert store.add_task("proj-b", "fix-bug", "/b/fix-bug.md") is True


def test_add_task_initial_state_is_pending(store):
    store.add_task("proj", "fix-bug", "/tasks/fix-bug.md")
    task = store.get_task("proj", "fix-bug")
    assert task is not None
    assert task.state == TaskState.PENDING
    assert task.retry_n == 0
    assert task.cost_usd == 0.0


def test_add_task_stores_model(store):
    store.add_task("proj", "fix-bug", "/tasks/fix-bug.md", model="claude-haiku-4-5-20251001")
    task = store.get_task("proj", "fix-bug")
    assert task.model == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# transition
# ---------------------------------------------------------------------------

def test_transition_succeeds_from_correct_state(store):
    store.add_task("proj", "fix-bug", "/tasks/fix-bug.md")
    result = store.transition("proj", "fix-bug", TaskState.PENDING, TaskState.EXPLORE)
    assert result is True
    task = store.get_task("proj", "fix-bug")
    assert task.state == TaskState.EXPLORE


def test_transition_returns_false_from_wrong_state(store):
    store.add_task("proj", "fix-bug", "/tasks/fix-bug.md")
    # Task is PENDING, try to transition from EXPLORE — should fail
    result = store.transition("proj", "fix-bug", TaskState.EXPLORE, TaskState.FIXING)
    assert result is False
    task = store.get_task("proj", "fix-bug")
    assert task.state == TaskState.PENDING  # unchanged


def test_transition_with_extra_columns(store):
    store.add_task("proj", "fix-bug", "/tasks/fix-bug.md")
    store.transition(
        "proj", "fix-bug",
        TaskState.PENDING, TaskState.EXPLORE,
        retry_n=1,
        branch="feature/fix-bug",
    )
    task = store.get_task("proj", "fix-bug")
    assert task.retry_n == 1
    assert task.branch == "feature/fix-bug"


def test_transition_explore_guide(store):
    store.add_task("proj", "fix-bug", "/tasks/fix-bug.md")
    store.transition("proj", "fix-bug", TaskState.PENDING, TaskState.EXPLORE)
    store.transition(
        "proj", "fix-bug",
        TaskState.EXPLORE, TaskState.FIXING,
        explore_guide="Root cause: missing null check\nFix: add guard at line 42",
    )
    task = store.get_task("proj", "fix-bug")
    assert task.state == TaskState.FIXING
    assert "null check" in task.explore_guide


def test_transition_rejects_unknown_column(store):
    store.add_task("proj", "fix-bug", "/tasks/fix-bug.md")
    with pytest.raises(ValueError, match="Unknown update columns"):
        store.transition("proj", "fix-bug", TaskState.PENDING, TaskState.EXPLORE, nonexistent="x")


def test_transition_atomic_double_dispatch(store):
    """Two threads racing to transition the same task — only one wins."""
    store.add_task("proj", "fix-bug", "/tasks/fix-bug.md")
    results = []

    def do_transition():
        r = store.transition("proj", "fix-bug", TaskState.PENDING, TaskState.EXPLORE)
        results.append(r)

    t1 = threading.Thread(target=do_transition)
    t2 = threading.Thread(target=do_transition)
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert results.count(True) == 1
    assert results.count(False) == 1


# ---------------------------------------------------------------------------
# cancel_task
# ---------------------------------------------------------------------------

def test_cancel_pending_task(store):
    store.add_task("proj", "fix-bug", "/tasks/fix-bug.md")
    assert store.cancel_task("proj", "fix-bug") is True
    task = store.get_task("proj", "fix-bug")
    assert task.state == TaskState.CANCELLED


def test_cancel_explore_task(store):
    store.add_task("proj", "fix-bug", "/tasks/fix-bug.md")
    store.transition("proj", "fix-bug", TaskState.PENDING, TaskState.EXPLORE)
    assert store.cancel_task("proj", "fix-bug") is True


def test_cancel_terminal_task_fails(store):
    store.add_task("proj", "fix-bug", "/tasks/fix-bug.md")
    store.transition("proj", "fix-bug", TaskState.PENDING, TaskState.PASSED)
    assert store.cancel_task("proj", "fix-bug") is False
    task = store.get_task("proj", "fix-bug")
    assert task.state == TaskState.PASSED  # unchanged


@pytest.mark.parametrize("terminal", list(TERMINAL_STATES))
def test_cancel_all_terminal_states_fail(store, terminal):
    store.add_task("proj", f"task-{terminal}", f"/tasks/task-{terminal}.md")
    # Force into terminal state via raw SQL (some terminal states aren't reachable via normal transitions)
    store._conn.execute(
        "UPDATE tasks SET state = ? WHERE project = ? AND slug = ?",
        (terminal, "proj", f"task-{terminal}"),
    )
    store._conn.commit()
    assert store.cancel_task("proj", f"task-{terminal}") is False


# ---------------------------------------------------------------------------
# next_task — priority ordering
# ---------------------------------------------------------------------------

def test_next_task_prefers_fixing_over_explore(store):
    store.add_task("proj", "task-a", "/a.md")
    store.add_task("proj", "task-b", "/b.md")
    store.transition("proj", "task-a", TaskState.PENDING, TaskState.EXPLORE)
    store.transition("proj", "task-b", TaskState.PENDING, TaskState.EXPLORE)
    store.transition("proj", "task-b", TaskState.EXPLORE, TaskState.FIXING)
    task = store.next_task("proj")
    assert task.slug == "task-b"
    assert task.state == TaskState.FIXING


def test_next_task_prefers_explore_over_pending(store):
    store.add_task("proj", "task-a", "/a.md")
    store.add_task("proj", "task-b", "/b.md")
    store.transition("proj", "task-a", TaskState.PENDING, TaskState.EXPLORE)
    task = store.next_task("proj")
    assert task.slug == "task-a"
    assert task.state == TaskState.EXPLORE


def test_next_task_returns_oldest_within_same_state(store):
    import time
    store.add_task("proj", "old-task", "/old.md")
    time.sleep(0.01)
    store.add_task("proj", "new-task", "/new.md")
    task = store.next_task("proj")
    assert task.slug == "old-task"


def test_next_task_returns_none_when_all_terminal(store):
    store.add_task("proj", "fix-bug", "/tasks/fix-bug.md")
    store.transition("proj", "fix-bug", TaskState.PENDING, TaskState.PASSED)
    assert store.next_task("proj") is None


def test_next_task_isolates_projects(store):
    store.add_task("proj-a", "fix-bug", "/a/fix-bug.md")
    store.add_task("proj-b", "other-task", "/b/other.md")
    store.transition("proj-a", "fix-bug", TaskState.PENDING, TaskState.PASSED)
    assert store.next_task("proj-a") is None
    assert store.next_task("proj-b") is not None


# ---------------------------------------------------------------------------
# list_tasks
# ---------------------------------------------------------------------------

def test_list_tasks_all_projects(store):
    store.add_task("proj-a", "task-1", "/a/1.md")
    store.add_task("proj-b", "task-2", "/b/2.md")
    tasks = store.list_tasks()
    assert len(tasks) == 2


def test_list_tasks_filtered_by_project(store):
    store.add_task("proj-a", "task-1", "/a/1.md")
    store.add_task("proj-b", "task-2", "/b/2.md")
    tasks = store.list_tasks("proj-a")
    assert len(tasks) == 1
    assert tasks[0].project == "proj-a"


# ---------------------------------------------------------------------------
# accumulate_cost
# ---------------------------------------------------------------------------

def test_accumulate_cost(store):
    store.add_task("proj", "fix-bug", "/tasks/fix-bug.md")
    store.accumulate_cost("proj", "fix-bug", 0.05)
    store.accumulate_cost("proj", "fix-bug", 0.03)
    task = store.get_task("proj", "fix-bug")
    assert task.cost_usd == pytest.approx(0.08)
