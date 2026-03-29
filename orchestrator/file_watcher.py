"""
Watches tasks/pending/ and tasks/active/ directories for new .md files
and inserts them into the StateStore.

Designed to be called once per scheduler poll cycle — it is idempotent.
"""

from pathlib import Path

from loguru import logger

import orchestrator.readiness_check as rc
import orchestrator.task_runner as tr
from orchestrator.scheduler import ProjectConfig
from orchestrator.state_store import StateStore, TaskState


class FileWatcher:
    """Syncs filesystem task directories → StateStore. Thread-safe (StateStore is thread-safe)."""

    def __init__(self, state_store: StateStore) -> None:
        self._store = state_store

    def scan_pending(self, project: ProjectConfig) -> int:
        """Insert any new .md files from tasks/pending/ as PENDING tasks.

        Returns the number of new tasks inserted.
        """
        pending_dir = project.repo_path / "tasks" / "pending"
        if not pending_dir.exists():
            return 0

        count = 0
        for spec in sorted(pending_dir.glob("*.md"), key=lambda p: p.stat().st_mtime):
            model = rc.parse_task_model(spec)
            added = self._store.add_task(project.name, spec.stem, str(spec), model)
            if added:
                logger.debug("[{}] Discovered new pending task: {}", project.name, spec.stem)
                count += 1
        return count

    def scan_active(self, project: ProjectConfig) -> int:
        """Adopt any .md files in tasks/active/ that aren't in the DB.

        These are legacy stuck tasks from before the SQLite migration.
        They are inserted and immediately transitioned to EXPLORE state.

        Returns the number of tasks adopted.
        """
        active_dir = project.repo_path / "tasks" / "active"
        if not active_dir.exists():
            return 0

        count = 0
        for spec in sorted(active_dir.glob("*.md"), key=lambda p: p.stat().st_mtime):
            slug = spec.stem
            if self._store.get_task(project.name, slug) is not None:
                continue  # already tracked
            model = rc.parse_task_model(spec)
            branch_type = tr.infer_branch_type(slug)
            branch_name = f"{branch_type}/{slug}"
            self._store.add_task(project.name, slug, str(spec), model)
            self._store.transition(
                project.name, slug,
                TaskState.PENDING, TaskState.EXPLORE,
                branch=branch_name, retry_n=1,
            )
            logger.info("[{}] Adopted legacy active task: {}", project.name, slug)
            count += 1
        return count

    def scan(self, project: ProjectConfig) -> int:
        """Run both scan_pending and scan_active. Returns total tasks inserted."""
        return self.scan_pending(project) + self.scan_active(project)
