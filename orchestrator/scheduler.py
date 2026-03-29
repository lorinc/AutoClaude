"""
Main loop. Polls projects in round-robin. Dispatches tasks based on state.

State dispatch priority (per project, per poll): FIXING > EXPLORE > PENDING.
All state transitions happen here; task_runner and git_manager are pure executors.
"""

import json
import signal
import time
from pathlib import Path
from typing import Callable

from loguru import logger
from pydantic import BaseModel

import orchestrator.cost_governor as cg
import orchestrator.git_manager as gm
import orchestrator.readiness_check as rc
import orchestrator.task_runner as tr
from orchestrator.state_store import StateStore, Task, TaskState

POLL_INTERVAL_DEFAULT = 60


class ProjectConfig(BaseModel):
    name: str
    repo_path: Path
    github_url: str
    staging_url: str = ""
    telegram_chat_id: str = ""
    python_version: str = "3.12"
    budget_limit_usd: float = 10.0
    explore_budget_fraction: float = 0.3  # fraction of budget_limit_usd for explore sessions

    model_config = {"extra": "ignore"}


def load_registry(registry_path: Path) -> list[ProjectConfig]:
    data = json.loads(registry_path.read_text())
    return [ProjectConfig(**p) for p in data]


# ---------------------------------------------------------------------------
# Filesystem helpers (legacy — used by file_watcher and tests)
# ---------------------------------------------------------------------------

def find_retry_task(repo_path: Path) -> str | None:
    """Return the slug of the oldest task in tasks/active/, or None."""
    active_dir = repo_path / "tasks" / "active"
    if not active_dir.exists():
        return None
    specs = sorted(active_dir.glob("*.md"), key=lambda p: p.stat().st_mtime)
    return specs[0].stem if specs else None


def find_pending_task(repo_path: Path) -> str | None:
    """Return the slug of the oldest task in tasks/pending/, or None."""
    pending_dir = repo_path / "tasks" / "pending"
    if not pending_dir.exists():
        return None
    specs = sorted(pending_dir.glob("*.md"), key=lambda p: p.stat().st_mtime)
    return specs[0].stem if specs else None


def _stub_notify_stuck(task_slug: str, retry_n: int, branch_name: str) -> None:
    logger.warning(
        "ESCALATED: task={} retry={} branch={} (Telegram not configured)",
        task_slug, retry_n, branch_name,
    )


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    def __init__(
        self,
        registry_path: Path,
        init_prompt: str,
        turn_limit: int,
        max_retries: int,
        poll_interval: int,
        dry_run: bool,
        state_store: StateStore | None = None,
        notify_stuck: Callable[[str, int, str], None] | None = None,
        costs_path: Path = Path("costs.jsonl"),
    ) -> None:
        self.registry_path = registry_path
        self.init_prompt = init_prompt
        self.turn_limit = turn_limit
        self.max_retries = max_retries
        self.poll_interval = poll_interval
        self.dry_run = dry_run
        self._state_store = state_store or StateStore(":memory:")
        self.notify_stuck = notify_stuck or _stub_notify_stuck
        self.costs_path = costs_path
        self._shutdown = False

    def _handle_sigterm(self, signum: int, frame: object) -> None:
        logger.info("SIGTERM received — will shut down after current task")
        self._shutdown = True

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        logger.info(
            "Scheduler started. poll_interval={}s dry_run={}",
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

    def _sync_fs_to_db(self, project: ProjectConfig) -> None:
        """Sync filesystem task dirs → state_store. Idempotent; safe to call each poll."""
        repo_path = project.repo_path

        # New pending files → PENDING in DB
        pending_dir = repo_path / "tasks" / "pending"
        if pending_dir.exists():
            for spec in sorted(pending_dir.glob("*.md"), key=lambda p: p.stat().st_mtime):
                model = rc.parse_task_model(spec)
                added = self._state_store.add_task(project.name, spec.stem, str(spec), model)
                if added:
                    logger.debug("[{}] Discovered new task: {}", project.name, spec.stem)

        # Active files without a DB entry → legacy stuck task, treat as EXPLORE
        active_dir = repo_path / "tasks" / "active"
        if active_dir.exists():
            for spec in sorted(active_dir.glob("*.md"), key=lambda p: p.stat().st_mtime):
                slug = spec.stem
                if self._state_store.get_task(project.name, slug) is None:
                    model = rc.parse_task_model(spec)
                    branch_type = tr.infer_branch_type(slug)
                    branch_name = f"{branch_type}/{slug}"
                    self._state_store.add_task(project.name, slug, str(spec), model)
                    self._state_store.transition(
                        project.name, slug,
                        TaskState.PENDING, TaskState.EXPLORE,
                        branch=branch_name, retry_n=1,
                    )
                    logger.info("[{}] Adopted legacy active task: {}", project.name, slug)

    def _process_project(self, project: ProjectConfig) -> None:
        repo_path = project.repo_path

        # Sync filesystem → DB (picks up files dropped by PO or from previous run)
        self._sync_fs_to_db(project)

        # Skip init_prompt for projects that already have CLAUDE.md
        is_scaffolded = (repo_path / "CLAUDE.md").exists()
        effective_prompt = "" if is_scaffolded else self.init_prompt

        task = self._state_store.next_task(project.name)
        if task is None:
            logger.debug("[{}] No actionable tasks.", project.name)
            return

        if task.state == TaskState.PENDING:
            self._dispatch_primary(task, project, effective_prompt)
        elif task.state == TaskState.EXPLORE:
            self._dispatch_explore(task, project, effective_prompt)
        elif task.state == TaskState.FIXING:
            self._dispatch_fix(task, project, effective_prompt)

    def _dispatch_primary(
        self,
        task: Task,
        project: ProjectConfig,
        init_prompt: str,
    ) -> None:
        repo_path = project.repo_path
        spec_path = Path(task.spec_path)

        readiness = rc.run(spec_path, repo_path, project.github_url, task.slug)
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
                    "[{}] Task '{}' blocked — unmerged branch exists.",
                    project.name, task.slug,
                )
            else:
                logger.warning("[{}] Task '{}' not ready: {}", project.name, task.slug, reason_str)
            return

        model = task.model or rc.parse_task_model(spec_path)
        branch_type = tr.infer_branch_type(task.slug)
        branch_name = f"{branch_type}/{task.slug}"

        logger.info("[{}] Dispatching primary: {} (model={})", project.name, task.slug, model)
        result = tr.run_primary_session(
            repo_path=repo_path,
            task_slug=task.slug,
            branch_name=branch_name,
            budget_usd=project.budget_limit_usd,
            init_prompt=init_prompt,
            turn_limit=self.turn_limit,
            dry_run=self.dry_run,
            model=model,
        )

        cg.log_cost(self.costs_path, cg.build_cost_record(
            project=project.name, task=task.slug, model=model,
            outcome=result.outcome, turns=result.turn_count, cost_usd=result.cost_usd,
        ))
        self._state_store.accumulate_cost(project.name, task.slug, result.cost_usd)

        if result.outcome == "pass":
            if not self.dry_run:
                gm.push_branch(repo_path, branch_name)
                tr.move_task(repo_path, task.slug, "active", "done")
            self._state_store.transition(
                project.name, task.slug, TaskState.PENDING, TaskState.PASSED,
                branch=branch_name, model=model,
            )
        elif result.outcome == "fail":
            if not self.dry_run:
                tr.move_task(repo_path, task.slug, "active", "done")
            self._state_store.transition(
                project.name, task.slug, TaskState.PENDING, TaskState.FAILED,
                branch=branch_name, model=model,
            )
        else:  # stuck
            if not self.dry_run:
                gm.commit_wip(repo_path, task.slug, 1)
                gm.promote_devlog_to_main(repo_path, task.slug, branch_name)
            self._state_store.transition(
                project.name, task.slug, TaskState.PENDING, TaskState.EXPLORE,
                branch=branch_name, model=model, retry_n=1,
            )

    def _dispatch_explore(
        self,
        task: Task,
        project: ProjectConfig,
        init_prompt: str,
    ) -> None:
        repo_path = project.repo_path
        model = task.model or "claude-sonnet-4-6"
        feature_branch = task.branch or f"{tr.infer_branch_type(task.slug)}/{task.slug}"
        sandbox_branch = f"sandbox/{task.slug}-{task.retry_n}"
        explore_budget = project.budget_limit_usd * project.explore_budget_fraction

        logger.info(
            "[{}] Dispatching explore: {} (retry={} budget=${:.2f})",
            project.name, task.slug, task.retry_n, explore_budget,
        )
        result, guide = tr.run_explore_session(
            repo_path=repo_path,
            task_slug=task.slug,
            feature_branch=feature_branch,
            sandbox_branch=sandbox_branch,
            budget_usd=explore_budget,
            explore_prompt=init_prompt,
            turn_limit=self.turn_limit,
            model=model,
        )

        cg.log_cost(self.costs_path, cg.build_cost_record(
            project=project.name, task=task.slug, model=model,
            outcome=f"explore:{result.outcome}", turns=result.turn_count, cost_usd=result.cost_usd,
        ))
        self._state_store.accumulate_cost(project.name, task.slug, result.cost_usd)

        if guide:
            self._state_store.transition(
                project.name, task.slug, TaskState.EXPLORE, TaskState.FIXING,
                explore_guide=guide,
            )
        else:
            logger.warning(
                "[{}] Explore session for '{}' produced no guide — escalating.",
                project.name, task.slug,
            )
            self.notify_stuck(task.slug, task.retry_n, feature_branch)
            if not self.dry_run:
                tr.move_task(repo_path, task.slug, "active", "done")
            self._state_store.transition(
                project.name, task.slug, TaskState.EXPLORE, TaskState.ESCALATED,
            )

    def _dispatch_fix(
        self,
        task: Task,
        project: ProjectConfig,
        init_prompt: str,
    ) -> None:
        repo_path = project.repo_path
        model = task.model or "claude-sonnet-4-6"
        feature_branch = task.branch or f"{tr.infer_branch_type(task.slug)}/{task.slug}"
        fix_budget = project.budget_limit_usd * (1.0 - project.explore_budget_fraction)

        logger.info(
            "[{}] Dispatching fix: {} (retry={} budget=${:.2f})",
            project.name, task.slug, task.retry_n, fix_budget,
        )
        result = tr.run_fix_session(
            repo_path=repo_path,
            task_slug=task.slug,
            feature_branch=feature_branch,
            budget_usd=fix_budget,
            base_prompt=init_prompt,
            explore_guide=task.explore_guide,
            turn_limit=self.turn_limit,
            model=model,
        )

        cg.log_cost(self.costs_path, cg.build_cost_record(
            project=project.name, task=task.slug, model=model,
            outcome=f"fix:{result.outcome}", turns=result.turn_count, cost_usd=result.cost_usd,
        ))
        self._state_store.accumulate_cost(project.name, task.slug, result.cost_usd)

        if result.outcome == "pass":
            if not self.dry_run:
                gm.push_branch(repo_path, feature_branch)
                tr.move_task(repo_path, task.slug, "active", "done")
            self._state_store.transition(
                project.name, task.slug, TaskState.FIXING, TaskState.PASSED,
            )
        else:  # stuck or fail
            next_retry = task.retry_n + 1
            if not self.dry_run:
                gm.commit_wip(repo_path, task.slug, next_retry)
                gm.promote_devlog_to_main(repo_path, task.slug, feature_branch)
            if next_retry >= self.max_retries:
                self.notify_stuck(task.slug, next_retry, feature_branch)
                if not self.dry_run:
                    tr.move_task(repo_path, task.slug, "active", "done")
                self._state_store.transition(
                    project.name, task.slug, TaskState.FIXING, TaskState.ESCALATED,
                )
            else:
                self._state_store.transition(
                    project.name, task.slug, TaskState.FIXING, TaskState.EXPLORE,
                    retry_n=next_retry,
                    explore_guide=None,  # reset — fresh explore next cycle
                )
