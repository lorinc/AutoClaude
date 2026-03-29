"""
Git subprocess operations for task management.

All functions raise subprocess.CalledProcessError on failure after logging
the stderr output. Callers should not suppress these exceptions — let them
propagate so the scheduler can handle and record the failure.
"""

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger


def _git(repo_path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(repo_path), *args]
    logger.debug("git {}", " ".join(args))
    try:
        return subprocess.run(cmd, check=check, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        logger.error("git {} failed (exit {}): {}", " ".join(args), e.returncode, e.stderr.strip())
        raise


# ---------------------------------------------------------------------------
# Branch helpers
# ---------------------------------------------------------------------------

def create_branch(repo_path: Path, branch_name: str) -> None:
    _git(repo_path, "checkout", "-b", branch_name)


def checkout_branch(repo_path: Path, branch_name: str) -> None:
    _git(repo_path, "checkout", branch_name)


def current_branch(repo_path: Path) -> str:
    return _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def delete_branch(repo_path: Path, branch_name: str, remote: bool = False) -> None:
    _git(repo_path, "branch", "-D", branch_name)
    if remote:
        _git(repo_path, "push", "origin", "--delete", branch_name, check=False)


def push_branch(repo_path: Path, branch_name: str) -> None:
    result = _git(repo_path, "push", "-u", "origin", branch_name, check=False)
    if result.returncode != 0:
        logger.warning("git push failed (non-fatal): {}", result.stderr.strip())


# ---------------------------------------------------------------------------
# Commit helpers
# ---------------------------------------------------------------------------

def commit_wip(repo_path: Path, slug: str, retry_n: int) -> None:
    """Commit any working state on the current branch. No-op if tree is clean."""
    status = _git(repo_path, "status", "--porcelain")
    if not status.stdout.strip():
        return
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-m", f"WIP: {slug} stuck attempt {retry_n}")


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


def get_main_devlog(repo_path: Path) -> str:
    """Return the content of main's devlog.md, or empty string if not found."""
    result = _git(repo_path, "show", "main:devlog.md", check=False)
    return result.stdout if result.returncode == 0 else ""


def get_retry_count(repo_path: Path, slug: str) -> int:
    """Count OUTCOME:stuck entries for this slug in main's devlog.md."""
    content = get_main_devlog(repo_path)
    return sum(
        1 for line in content.splitlines()
        if f"TASK:{slug}" in line and "OUTCOME:stuck" in line
    )


def promote_devlog_to_main(repo_path: Path, slug: str, branch_name: str) -> None:
    """Copy devlog entries added on the feature branch onto main's devlog.md.

    Must be called before any branch deletion so lessons survive.
    Assumes the working tree is clean (call after commit_wip).
    """
    diff = _git(repo_path, "diff", f"main..{branch_name}", "--", "devlog.md", check=False)
    if diff.returncode != 0 or not diff.stdout.strip():
        return

    new_lines = [
        line[1:] for line in diff.stdout.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    if not new_lines:
        return

    saved = current_branch(repo_path)
    _git(repo_path, "checkout", "main")
    try:
        devlog = repo_path / "devlog.md"
        with devlog.open("a") as f:
            f.write("\n".join(new_lines) + "\n")
        _git(repo_path, "add", "devlog.md")
        _git(repo_path, "commit", "-m", f"preserve lessons: {slug} stuck attempt")
    finally:
        _git(repo_path, "checkout", saved)
