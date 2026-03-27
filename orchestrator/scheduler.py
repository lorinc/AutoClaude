"""
Main loop. Polls projects in round-robin. Dispatches tasks to task_runner.
Phase 3c.
"""

import json
import logging
import signal
import time
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

import orchestrator.readiness_check as rc
import orchestrator.task_runner as tr

logger = logging.getLogger(__name__)

POLL_INTERVAL_DEFAULT = 60


class ProjectConfig(BaseModel):
    name: str
    repo_path: Path
    github_url: str
    staging_url: str = ""
    telegram_chat_id: str = ""
    python_version: str = "3.12"
    budget_limit_usd: float = 10.0

    model_config = {"extra": "ignore"}


def load_registry(registry_path: Path) -> list[ProjectConfig]:
    data = json.loads(registry_path.read_text())
    return [ProjectConfig(**p) for p in data]


def find_retry_task(repo_path: Path) -> str | None:
    """Return the slug of the oldest task awaiting retry in tasks/active/, or None."""
    active_dir = repo_path / "tasks" / "active"
    if not active_dir.exists():
        return None
    specs = sorted(active_dir.glob("*.md"), key=lambda p: p.stat().st_mtime)
    return specs[0].stem if specs else None


def find_pending_task(repo_path: Path) -> str | None:
    """Return the oldest pending task slug, or None."""
    pending_dir = repo_path / "tasks" / "pending"
    if not pending_dir.exists():
        return None
    specs = sorted(pending_dir.glob("*.md"), key=lambda p: p.stat().st_mtime)
    return specs[0].stem if specs else None


def _stub_notify_stuck(task_slug: str, retry_n: int, branch_name: str) -> None:
    logger.warning(
        "STUCK: task=%s retry=%d branch=%s (Telegram not wired yet)",
        task_slug, retry_n, branch_name,
    )


class Scheduler:
    def __init__(
        self,
        registry_path: Path,
        init_prompt: str,
        turn_limit: int,
        max_retries: int,
        poll_interval: int,
        dry_run: bool,
        notify_stuck: Callable[[str, int, str], None] | None = None,
    ) -> None:
        self.registry_path = registry_path
        self.init_prompt = init_prompt
        self.turn_limit = turn_limit
        self.max_retries = max_retries
        self.poll_interval = poll_interval
        self.dry_run = dry_run
        self.notify_stuck = notify_stuck or _stub_notify_stuck
        self._shutdown = False

    def _handle_sigterm(self, signum: int, frame: object) -> None:
        logger.info("SIGTERM received — will shut down after current task")
        self._shutdown = True

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        logger.info(
            "Scheduler started. poll_interval=%ds dry_run=%s",
            self.poll_interval, self.dry_run,
        )

        while not self._shutdown:
            projects = load_registry(self.registry_path)
            if not projects:
                logger.info("No projects registered. Sleeping.")
            else:
                for project in projects:
                    if self._shutdown:
                        break
                    self._process_project(project)

            if not self._shutdown:
                time.sleep(self.poll_interval)

        logger.info("Scheduler shut down cleanly.")

    def _process_project(self, project: ProjectConfig) -> None:
        repo_path = project.repo_path

        # Priority 1: stuck task awaiting retry in tasks/active/
        retry_slug = find_retry_task(repo_path)
        if retry_slug:
            logger.info("[%s] Retrying stuck task: %s", project.name, retry_slug)
            tr.run_task(
                repo_path=repo_path,
                task_slug=retry_slug,
                budget_usd=project.budget_limit_usd,
                init_prompt=self.init_prompt,
                turn_limit=self.turn_limit,
                dry_run=self.dry_run,
                notify_stuck=self.notify_stuck,
                max_retries=self.max_retries,
                is_retry=True,
            )
            return

        # Priority 2: new pending task — runs Definition of Ready check first
        pending_slug = find_pending_task(repo_path)
        if not pending_slug:
            logger.debug("[%s] No pending tasks.", project.name)
            return

        spec_path = repo_path / "tasks" / "pending" / f"{pending_slug}.md"
        readiness = rc.run(spec_path, repo_path, project.github_url, pending_slug)

        if not readiness.ready:
            reasons = []
            for field in ("task_spec", "branch_clean", "no_duplicate_pr", "ci_green"):
                check = getattr(readiness, field)
                if not check.passed:
                    reasons.append(f"{field}: {check.reason}")
            reason_str = "; ".join(reasons)
            if not readiness.branch_clean.passed and "Unmerged agent branches" in (
                readiness.branch_clean.reason or ""
            ):
                logger.info(
                    "[%s] Ad-hoc task '%s' blocked — unmerged branch exists. Will retry when trunk is clean.",
                    project.name, pending_slug,
                )
            else:
                logger.warning(
                    "[%s] Task '%s' not ready: %s", project.name, pending_slug, reason_str,
                )
            return

        logger.info("[%s] Dispatching task: %s", project.name, pending_slug)
        tr.run_task(
            repo_path=repo_path,
            task_slug=pending_slug,
            budget_usd=project.budget_limit_usd,
            init_prompt=self.init_prompt,
            turn_limit=self.turn_limit,
            dry_run=self.dry_run,
            notify_stuck=self.notify_stuck,
            max_retries=self.max_retries,
            is_retry=False,
        )
