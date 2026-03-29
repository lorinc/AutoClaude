"""
Session execution for AutoClaude tasks.

Provides three session runners (primary / explore / fix) and the low-level
run_session() that manages the Claude Code subprocess.

Git operations are delegated to git_manager. State transitions are the
responsibility of the calling scheduler — these functions return SessionResult
and leave state management to their callers.

Session types (passed via AUTOCLAUDE_SESSION_TYPE env var):
  primary  — first attempt at a task
  explore  — sandbox exploration after primary stuck/failed
  fix      — targeted fix guided by explore_guide from DB
"""

import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Literal

from loguru import logger
from pydantic import BaseModel

import orchestrator.git_manager as gm

AGENT_BRANCH_KEYWORDS: dict[str, tuple[str, ...]] = {
    "fix":   ("fix", "bug", "patch", "hotfix"),
    "chore": ("chore", "cleanup", "refactor", "deps", "lint", "format", "bump"),
}

TASK_PROMPT = (
    "Begin. Follow the context loading protocol in CLAUDE.md and complete the active task."
)

# Guide file written by explore sessions (relative to repo root)
GUIDE_FILENAME = "tasks/active/{slug}.guide.md"


class SessionResult(BaseModel):
    outcome: Literal["pass", "fail", "stuck"]
    turn_count: int
    exit_code: int
    note: str
    cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def infer_branch_type(task_slug: str) -> str:
    slug_lower = task_slug.lower()
    for branch_type, keywords in AGENT_BRANCH_KEYWORDS.items():
        if any(kw in slug_lower for kw in keywords):
            return branch_type
    return "feature"


def move_task(repo_path: Path, slug: str, from_dir: str, to_dir: str) -> Path:
    """Move a task .md file between tasks/ subdirectories (filesystem side-effect only)."""
    src = repo_path / "tasks" / from_dir / f"{slug}.md"
    dst_dir = repo_path / "tasks" / to_dir
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{slug}.md"
    src.rename(dst)
    return dst


# ---------------------------------------------------------------------------
# Invocation builder
# ---------------------------------------------------------------------------

def build_invocation(
    budget_usd: float,
    init_prompt: str = "",
    model: str = "claude-sonnet-4-6",
    session_type: str = "primary",
) -> list[str]:
    cmd = [
        "claude", "-p",
        "--dangerously-skip-permissions",
        "--verbose",
        "--max-budget-usd", str(budget_usd),
        "--output-format", "stream-json",
        "--model", model,
    ]
    if init_prompt:
        cmd += ["--append-system-prompt", init_prompt]
    cmd.append(TASK_PROMPT)
    return cmd


# ---------------------------------------------------------------------------
# Core subprocess runner
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
    session_type: str = "primary",
) -> SessionResult:
    env = {
        **os.environ,
        "AUTOCLAUDE_TASK_SLUG": task_slug,
        "AUTOCLAUDE_REPO_PATH": str(repo_path),
        "AUTOCLAUDE_SESSION_TYPE": session_type,
    }
    logger.info("Starting {} session for {}: {}", session_type, task_slug, " ".join(invocation[:4]))
    process = subprocess.Popen(
        invocation,
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,  # isolate process group so SIGTERM reaches all children
    )

    turn_count = 0
    killed = False
    last_result_event: dict | None = None
    for line in process.stdout:
        turn_count += _count_tool_uses(line)
        if not killed and turn_count >= turn_limit:
            logger.info("Turn limit ({}) reached for {} — sending SIGTERM", turn_limit, task_slug)
            _kill_process_group(process, signal.SIGTERM)
            killed = True
        try:
            ev = json.loads(line)
            if ev.get("type") == "result":
                last_result_event = ev
        except json.JSONDecodeError:
            pass

    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        logger.warning("Process did not exit after 30s — force killing {}", task_slug)
        _kill_process_group(process, signal.SIGKILL)
        process.wait()

    cost_usd = float(last_result_event.get("total_cost_usd") or 0.0) if last_result_event else 0.0
    logger.info(
        "Session ended: slug={} outcome={} turns={} cost=${:.4f}",
        task_slug,
        "stuck" if killed else ("fail" if process.returncode != 0 else "pass"),
        turn_count, cost_usd,
    )

    if killed:
        return SessionResult(
            outcome="stuck",
            turn_count=turn_count,
            exit_code=process.returncode,
            note=f"Turn limit ({turn_limit}) reached",
            cost_usd=cost_usd,
        )
    if process.returncode != 0:
        return SessionResult(
            outcome="fail",
            turn_count=turn_count,
            exit_code=process.returncode,
            note=f"Session exited with code {process.returncode}",
            cost_usd=cost_usd,
        )
    return SessionResult(outcome="pass", turn_count=turn_count, exit_code=0, note="Session completed", cost_usd=cost_usd)


def _kill_process_group(process: subprocess.Popen, sig: int) -> None:
    """Send *sig* to the process group. Best-effort — ignores ProcessLookupError."""
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except ProcessLookupError:
        pass  # already exited


# ---------------------------------------------------------------------------
# Session runners (called by scheduler)
# ---------------------------------------------------------------------------

def run_primary_session(
    repo_path: Path,
    task_slug: str,
    branch_name: str,
    budget_usd: float,
    init_prompt: str,
    turn_limit: int,
    dry_run: bool,
    model: str = "claude-sonnet-4-6",
) -> SessionResult:
    """First attempt at a new task. Creates branch and runs the session."""
    gm.create_branch(repo_path, branch_name)
    move_task(repo_path, task_slug, "pending", "active")

    invocation = build_invocation(budget_usd, init_prompt, model, session_type="primary")

    if dry_run:
        logger.info("[DRY_RUN] Would run: {}", " ".join(invocation))
        move_task(repo_path, task_slug, "active", "done")
        return SessionResult(outcome="pass", turn_count=0, exit_code=0, note="dry run")

    result = run_session(repo_path, invocation, task_slug, turn_limit, session_type="primary")
    gm.append_devlog(repo_path, task_slug, result.outcome, result.note)
    return result


def run_explore_session(
    repo_path: Path,
    task_slug: str,
    feature_branch: str,
    sandbox_branch: str,
    budget_usd: float,
    explore_prompt: str,
    turn_limit: int,
    model: str = "claude-sonnet-4-6",
) -> tuple[SessionResult, str | None]:
    """Sandbox exploration session.

    Branches from *feature_branch* into *sandbox_branch*, runs the session,
    reads the guide file if written, then returns to *feature_branch* and
    deletes the sandbox branch.

    Returns (SessionResult, guide_content_or_None).
    """
    gm.checkout_branch(repo_path, feature_branch)
    gm.create_branch(repo_path, sandbox_branch)

    invocation = build_invocation(budget_usd, explore_prompt, model, session_type="explore")
    result = run_session(repo_path, invocation, task_slug, turn_limit, session_type="explore")
    gm.append_devlog(repo_path, task_slug, result.outcome, f"explore: {result.note}")

    guide_path = repo_path / GUIDE_FILENAME.format(slug=task_slug)
    guide_content: str | None = None
    if guide_path.exists():
        guide_content = guide_path.read_text()
        guide_path.unlink()
        logger.info("Explore guide captured ({} chars) for {}", len(guide_content), task_slug)
    else:
        logger.warning("No guide file found after explore session for {}", task_slug)

    gm.checkout_branch(repo_path, feature_branch)
    try:
        gm.delete_branch(repo_path, sandbox_branch)
    except subprocess.CalledProcessError:
        logger.warning("Could not delete sandbox branch {}", sandbox_branch)

    return result, guide_content


def run_fix_session(
    repo_path: Path,
    task_slug: str,
    feature_branch: str,
    budget_usd: float,
    base_prompt: str,
    explore_guide: str | None,
    turn_limit: int,
    model: str = "claude-sonnet-4-6",
) -> SessionResult:
    """Targeted fix session using the explore guide."""
    gm.checkout_branch(repo_path, feature_branch)

    fix_prompt = base_prompt
    if explore_guide:
        fix_prompt = f"{base_prompt}\n\n## Exploration Guide\n{explore_guide}"

    invocation = build_invocation(budget_usd, fix_prompt, model, session_type="fix")
    result = run_session(repo_path, invocation, task_slug, turn_limit, session_type="fix")
    gm.append_devlog(repo_path, task_slug, result.outcome, f"fix: {result.note}")
    return result
