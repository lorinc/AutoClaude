"""
Picks up a task, creates a branch, runs a Claude Code session, handles the outcome.
Phase 3b.

Devlog guarantee model:
- Agent writes rich notes to devlog.md during the session (LLM-driven, not enforced here)
- Claude Code Stop hook (hooks/devlog-guard.sh) appends a minimal entry on clean exit
- This module always appends an entry after every session (covers SIGTERM / kill)
- On stuck: WIP committed on branch, devlog entries promoted to main before any branch deletion
- Retry count derived from main's devlog.md — no separate state file, survives branch deletion
"""

import json
import os
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel

AGENT_BRANCH_KEYWORDS: dict[str, tuple[str, ...]] = {
    "fix":   ("fix", "bug", "patch", "hotfix"),
    "chore": ("chore", "cleanup", "refactor", "deps", "lint", "format", "bump"),
}

TASK_PROMPT = (
    "Begin. Follow the context loading protocol in CLAUDE.md and complete the active task."
)


class SessionResult(BaseModel):
    outcome: Literal["pass", "fail", "stuck"]
    turn_count: int
    exit_code: int
    note: str


# ---------------------------------------------------------------------------
# Branch helpers
# ---------------------------------------------------------------------------

def infer_branch_type(task_slug: str) -> str:
    slug_lower = task_slug.lower()
    for branch_type, keywords in AGENT_BRANCH_KEYWORDS.items():
        if any(kw in slug_lower for kw in keywords):
            return branch_type
    return "feature"


def create_branch(repo_path: Path, branch_name: str) -> None:
    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=repo_path, check=True, capture_output=True,
    )


def checkout_branch(repo_path: Path, branch_name: str) -> None:
    subprocess.run(
        ["git", "checkout", branch_name],
        cwd=repo_path, check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# Task file helpers
# ---------------------------------------------------------------------------

def move_task(repo_path: Path, slug: str, from_dir: str, to_dir: str) -> Path:
    src = repo_path / "tasks" / from_dir / f"{slug}.md"
    dst_dir = repo_path / "tasks" / to_dir
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{slug}.md"
    src.rename(dst)
    return dst


# ---------------------------------------------------------------------------
# Devlog helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def append_devlog(repo_path: Path, slug: str, outcome: str, note: str) -> None:
    devlog = repo_path / "devlog.md"
    line = f"[{_now()}] TASK:{slug} OUTCOME:{outcome} NOTE:{note}\n"
    with devlog.open("a") as f:
        f.write(line)


def get_retry_count(repo_path: Path, slug: str) -> int:
    """Count OUTCOME:stuck entries for this slug in main's devlog.md.

    Reading from main (not the current branch) ensures the count reflects
    promoted lessons only — not in-progress entries on the feature branch.
    """
    result = subprocess.run(
        ["git", "show", "main:devlog.md"],
        cwd=repo_path, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return 0
    return sum(
        1 for line in result.stdout.splitlines()
        if f"TASK:{slug}" in line and "OUTCOME:stuck" in line
    )


def promote_devlog_to_main(repo_path: Path, slug: str, branch_name: str) -> None:
    """Copy devlog entries added on the feature branch onto main.

    Must be called before any branch deletion so lessons survive.
    Assumes the working tree is clean (call after commit_wip).
    """
    diff = subprocess.run(
        ["git", "diff", f"main..{branch_name}", "--", "devlog.md"],
        cwd=repo_path, capture_output=True, text=True,
    )
    if diff.returncode != 0 or not diff.stdout.strip():
        return

    new_lines = [
        line[1:] for line in diff.stdout.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    if not new_lines:
        return

    current = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_path, capture_output=True, text=True,
    ).stdout.strip()

    subprocess.run(["git", "checkout", "main"], cwd=repo_path, check=True, capture_output=True)
    devlog = repo_path / "devlog.md"
    with devlog.open("a") as f:
        f.write("\n".join(new_lines) + "\n")
    subprocess.run(["git", "add", "devlog.md"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"preserve lessons: {slug} stuck attempt"],
        cwd=repo_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", current], cwd=repo_path, check=True, capture_output=True
    )


def commit_wip(repo_path: Path, slug: str, retry_n: int) -> None:
    """Commit any working state on the current branch. No-op if tree is clean."""
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path, capture_output=True, text=True,
    )
    if not status.stdout.strip():
        return
    subprocess.run(["git", "add", "-A"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"WIP: {slug} stuck attempt {retry_n}"],
        cwd=repo_path, check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# Invocation builder
# ---------------------------------------------------------------------------

def build_invocation(
    budget_usd: float,
    init_prompt: str,
    model: str = "claude-sonnet-4-6",
) -> list[str]:
    return [
        "claude", "-p",
        "--dangerously-skip-permissions",
        "--max-budget-usd", str(budget_usd),
        "--output-format", "stream-json",
        "--model", model,
        "--append-system-prompt", init_prompt,
        TASK_PROMPT,
    ]


# ---------------------------------------------------------------------------
# Session runner
# ---------------------------------------------------------------------------

def _count_tool_uses(line: str) -> int:
    """Count tool_use blocks in one stream-json line."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return 0
    if event.get("type") != "assistant":
        return 0
    content = event.get("message", {}).get("content", [])
    return sum(1 for block in content if isinstance(block, dict) and block.get("type") == "tool_use")


def run_session(
    repo_path: Path,
    invocation: list[str],
    task_slug: str,
    turn_limit: int,
) -> SessionResult:
    env = {
        **os.environ,
        "AUTOCLAUDE_TASK_SLUG": task_slug,
        "AUTOCLAUDE_REPO_PATH": str(repo_path),
    }
    process = subprocess.Popen(
        invocation,
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    turn_count = 0
    killed = False
    for line in process.stdout:
        turn_count += _count_tool_uses(line)
        if not killed and turn_count >= turn_limit:
            process.send_signal(signal.SIGTERM)
            killed = True

    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()

    if killed:
        return SessionResult(
            outcome="stuck",
            turn_count=turn_count,
            exit_code=process.returncode,
            note=f"Turn limit ({turn_limit}) reached",
        )
    if process.returncode != 0:
        return SessionResult(
            outcome="fail",
            turn_count=turn_count,
            exit_code=process.returncode,
            note=f"Session exited with code {process.returncode}",
        )
    return SessionResult(outcome="pass", turn_count=turn_count, exit_code=0, note="Session completed")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_task(
    repo_path: Path,
    task_slug: str,
    budget_usd: float,
    init_prompt: str,
    turn_limit: int,
    dry_run: bool,
    notify_stuck: Callable[[str, int, str], None],
    max_retries: int = 3,
    is_retry: bool = False,
) -> SessionResult:
    branch_type = infer_branch_type(task_slug)
    branch_name = f"{branch_type}/{task_slug}"

    if is_retry:
        checkout_branch(repo_path, branch_name)
    else:
        move_task(repo_path, task_slug, "pending", "active")
        create_branch(repo_path, branch_name)

    invocation = build_invocation(budget_usd, init_prompt)

    if dry_run:
        print(f"[DRY_RUN] cwd={repo_path}")
        print(f"[DRY_RUN] {' '.join(invocation)}")
        move_task(repo_path, task_slug, "active", "done")
        return SessionResult(outcome="pass", turn_count=0, exit_code=0, note="dry run")

    result = run_session(repo_path, invocation, task_slug, turn_limit)

    # Orchestrator always appends devlog — covers the SIGTERM case where the agent was killed
    append_devlog(repo_path, task_slug, result.outcome, result.note)

    if result.outcome in ("pass", "fail"):
        move_task(repo_path, task_slug, "active", "done")
        return result

    # Stuck: commit WIP, promote lessons to main, then decide retry vs. escalate
    retry_n = get_retry_count(repo_path, task_slug) + 1
    commit_wip(repo_path, task_slug, retry_n)
    promote_devlog_to_main(repo_path, task_slug, branch_name)

    if retry_n >= max_retries:
        notify_stuck(task_slug, retry_n, branch_name)
        move_task(repo_path, task_slug, "active", "done")
    # else: task stays in active/ — scheduler picks it up for retry

    return result
