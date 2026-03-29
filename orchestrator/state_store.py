"""
SQLite-backed task state machine.

Provides atomic state transitions to prevent double-dispatch and stuck-file bugs.
All write operations are protected by a threading.Lock so the Scheduler and
TelegramCommandDispatcher can safely share one StateStore instance.
"""

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path


class TaskState(StrEnum):
    PENDING   = "PENDING"
    EXPLORE   = "EXPLORE"   # primary session stuck/failed → explore sandbox next
    FIXING    = "FIXING"    # explore done, guide saved → targeted fix next
    PASSED    = "PASSED"
    FAILED    = "FAILED"
    ESCALATED = "ESCALATED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES: frozenset[TaskState] = frozenset({
    TaskState.PASSED,
    TaskState.FAILED,
    TaskState.ESCALATED,
    TaskState.CANCELLED,
})

# Ordered by dispatch priority (highest first)
ACTIONABLE_STATES: list[TaskState] = [
    TaskState.FIXING,
    TaskState.EXPLORE,
    TaskState.PENDING,
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project       TEXT    NOT NULL,
    slug          TEXT    NOT NULL,
    state         TEXT    NOT NULL DEFAULT 'PENDING',
    retry_n       INTEGER NOT NULL DEFAULT 0,
    model         TEXT,
    branch        TEXT,
    spec_path     TEXT    NOT NULL,
    explore_guide TEXT,
    cost_usd      REAL    NOT NULL DEFAULT 0.0,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    UNIQUE(project, slug)
);
"""

_ALLOWED_UPDATE_COLS = frozenset({"branch", "retry_n", "explore_guide", "cost_usd", "model"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Task:
    id: int
    project: str
    slug: str
    state: TaskState
    retry_n: int
    model: str | None
    branch: str | None
    spec_path: str
    explore_guide: str | None
    cost_usd: float
    created_at: str
    updated_at: str


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        project=row["project"],
        slug=row["slug"],
        state=TaskState(row["state"]),
        retry_n=row["retry_n"],
        model=row["model"],
        branch=row["branch"],
        spec_path=row["spec_path"],
        explore_guide=row["explore_guide"],
        cost_usd=row["cost_usd"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class StateStore:
    """Thread-safe SQLite-backed task state machine."""

    def __init__(self, db_path: Path | str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def add_task(
        self,
        project: str,
        slug: str,
        spec_path: str,
        model: str | None = None,
    ) -> bool:
        """Insert a new PENDING task. Returns True if inserted, False if slug already exists."""
        now = _now()
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO tasks
                        (project, slug, state, model, spec_path, created_at, updated_at)
                    VALUES (?, ?, 'PENDING', ?, ?, ?, ?)
                    """,
                    (project, slug, model, spec_path, now, now),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def transition(
        self,
        project: str,
        slug: str,
        from_state: TaskState,
        to_state: TaskState,
        **updates: object,
    ) -> bool:
        """Atomically transition a task from *from_state* to *to_state*.

        Extra columns to update alongside the state change can be supplied as
        keyword arguments (only columns in _ALLOWED_UPDATE_COLS are accepted).

        Returns True if the row was updated, False if the task was not in
        *from_state* (i.e. another transition already happened).
        """
        bad = set(updates) - _ALLOWED_UPDATE_COLS
        if bad:
            raise ValueError(f"Unknown update columns: {bad}")

        set_clauses = ["state = ?", "updated_at = ?"]
        params: list[object] = [to_state, _now()]
        for col, val in updates.items():
            set_clauses.append(f"{col} = ?")
            params.append(val)
        params.extend([project, slug, from_state])

        with self._lock:
            cur = self._conn.execute(
                f"UPDATE tasks SET {', '.join(set_clauses)} "
                f"WHERE project = ? AND slug = ? AND state = ?",
                params,
            )
            self._conn.commit()
            return cur.rowcount == 1

    def cancel_task(self, project: str, slug: str) -> bool:
        """Cancel a non-terminal task. Returns True if the task was cancelled."""
        terminal = ", ".join(f"'{s}'" for s in TERMINAL_STATES)
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                f"""
                UPDATE tasks SET state = 'CANCELLED', updated_at = ?
                WHERE project = ? AND slug = ?
                  AND state NOT IN ({terminal})
                """,
                (now, project, slug),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def accumulate_cost(self, project: str, slug: str, cost_usd: float) -> None:
        """Add *cost_usd* to the task's running total."""
        now = _now()
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET cost_usd = cost_usd + ?, updated_at = ? "
                "WHERE project = ? AND slug = ?",
                (cost_usd, now, project, slug),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def next_task(self, project: str) -> Task | None:
        """Return the oldest actionable task for a project (FIXING > EXPLORE > PENDING)."""
        for state in ACTIONABLE_STATES:
            row = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE project = ? AND state = ?
                ORDER BY updated_at ASC, id ASC
                LIMIT 1
                """,
                (project, state),
            ).fetchone()
            if row:
                return _row_to_task(row)
        return None

    def get_task(self, project: str, slug: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE project = ? AND slug = ?",
            (project, slug),
        ).fetchone()
        return _row_to_task(row) if row else None

    def list_tasks(self, project: str | None = None) -> list[Task]:
        if project:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE project = ? ORDER BY updated_at DESC",
                (project,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY updated_at DESC"
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
