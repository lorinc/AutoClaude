"""
Telegram notification and command dispatch.

Architecture:
- Low-level HTTP primitives (send_message, get_updates) are stateless.
- TelegramCommandDispatcher runs as a background daemon thread alongside the
  scheduler, polling for commands and dispatching to handlers.
- Handoff sessions spawn a named tmux window at the project's repo path so
  the developer can attach from their local terminal (SSH + tmux attach).

Commands:
  /status              — orchestrator uptime, task counts
  /list [project]      — list tasks with their state
  /start <proj> <slug> — force-trigger a task from pending to next dispatch
  /cancel <slug>       — cancel a task
  /approve             — approve AWAITING_APPROVAL plan
  /reject [reason]     — reject AWAITING_APPROVAL plan
  /hint <slug> <text>  — append hint to explore_guide for next session
  /session <slug>      — spawn tmux handoff session, send attach command
  /kill                — SIGTERM orchestrator after current task
"""

import os
import subprocess
import threading
import time
from typing import Callable, NamedTuple
from pathlib import Path

import httpx
from loguru import logger

from orchestrator.state_store import StateStore, TaskState

_API = "https://api.telegram.org/bot{token}/{method}"


# ---------------------------------------------------------------------------
# Low-level primitives
# ---------------------------------------------------------------------------

def send_message(token: str, chat_id: str, text: str) -> None:
    """POST a text message. Raises on non-2xx."""
    url = _API.format(token=token, method="sendMessage")
    resp = httpx.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    resp.raise_for_status()


def get_updates(token: str, offset: int = 0, timeout: int = 30) -> list[dict]:
    """Long-poll for new updates. Returns the result list."""
    url = _API.format(token=token, method="getUpdates")
    resp = httpx.get(
        url,
        params={"offset": offset, "timeout": timeout},
        timeout=timeout + 5,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------

def notify_stuck(
    token: str,
    chat_id: str,
    task_slug: str,
    retry_n: int,
    branch_name: str,
) -> None:
    text = (
        f"\u26a0\ufe0f ESCALATED: {task_slug} (attempt {retry_n})\n"
        f"Branch: {branch_name}\n"
        f"Take over:\n  tmux attach -t autoclaude-{task_slug}\n"
        "(SSH to server first if remote)"
    )
    send_message(token, chat_id, text)


def notify_uat_ready(
    token: str,
    chat_id: str,
    project_name: str,
    staging_url: str,
    changelog: str,
) -> None:
    text = (
        f"\u2705 UAT ready: {project_name}\n"
        f"Staging: {staging_url}\n\n"
        f"{changelog}"
    )
    send_message(token, chat_id, text)


# ---------------------------------------------------------------------------
# Approval result (used by scheduler for AWAITING_APPROVAL tasks)
# ---------------------------------------------------------------------------

class ApprovalResult(NamedTuple):
    approved: bool
    reason: str | None


# ---------------------------------------------------------------------------
# Handoff session (tmux)
# ---------------------------------------------------------------------------

def spawn_handoff_session(
    repo_path: Path,
    task_slug: str,
    shell: str = "fish",
) -> str:
    """Spawn a named tmux session at repo_path for human takeover.

    Returns the session name. The caller should send the attach command via Telegram.
    """
    session_name = f"autoclaude-{task_slug}"
    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, "-c", str(repo_path), shell],
            check=True, capture_output=True,
        )
        logger.info("Spawned handoff session: {}", session_name)
    except subprocess.CalledProcessError as e:
        logger.warning("Could not create tmux session '{}': {}", session_name, e.stderr.decode())
        raise
    return session_name


# ---------------------------------------------------------------------------
# TelegramCommandDispatcher
# ---------------------------------------------------------------------------

class TelegramCommandDispatcher(threading.Thread):
    """Background thread that polls Telegram and dispatches commands.

    Non-blocking with respect to the scheduler — runs independently.
    Approval results are stored in _pending_approval and read by the scheduler.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        state_store: StateStore,
        registry_path: Path,
        handoff_shell: str = "fish",
    ) -> None:
        super().__init__(name="telegram-dispatcher", daemon=True)
        self._token = token
        self._chat_id = chat_id
        self._store = state_store
        self._registry_path = registry_path
        self._handoff_shell = handoff_shell
        self._offset = 0
        self._stop_event = threading.Event()
        # Pending approval: dict[slug, ApprovalResult | None]
        self._pending_approval: dict[str, ApprovalResult | None] = {}
        self._approval_lock = threading.Lock()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        logger.info("Telegram dispatcher started")
        while not self._stop_event.is_set():
            try:
                updates = get_updates(self._token, offset=self._offset, timeout=10)
                for update in updates:
                    self._offset = update["update_id"] + 1
                    self._handle_update(update)
            except Exception:
                logger.exception("Telegram dispatcher error — will retry")
                time.sleep(5)

    def _handle_update(self, update: dict) -> None:
        msg = update.get("message", {})
        if str(msg.get("chat", {}).get("id", "")) != str(self._chat_id):
            return  # ignore messages from other chats
        text = msg.get("text", "").strip()
        if not text.startswith("/"):
            return

        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        handlers = {
            "/status":  self._cmd_status,
            "/list":    self._cmd_list,
            "/start":   self._cmd_start,
            "/cancel":  self._cmd_cancel,
            "/approve": self._cmd_approve,
            "/reject":  self._cmd_reject,
            "/hint":    self._cmd_hint,
            "/session": self._cmd_session,
            "/kill":    self._cmd_kill,
        }
        handler = handlers.get(command, self._cmd_unknown)
        try:
            handler(args)
        except Exception:
            logger.exception("Error handling Telegram command: {}", text)
            self._reply("Error handling command — check logs.")

    def _reply(self, text: str) -> None:
        try:
            send_message(self._token, self._chat_id, text)
        except Exception:
            logger.exception("Failed to send Telegram reply")

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def _cmd_status(self, _args: str) -> None:
        all_tasks = self._store.list_tasks()
        by_state: dict[str, int] = {}
        for task in all_tasks:
            by_state[task.state] = by_state.get(task.state, 0) + 1
        lines = ["\U0001f916 Orchestrator status"]
        for state, count in sorted(by_state.items()):
            lines.append(f"  {state}: {count}")
        lines.append(f"\nTotal tasks: {len(all_tasks)}")
        self._reply("\n".join(lines))

    def _cmd_list(self, args: str) -> None:
        project = args.strip() or None
        tasks = self._store.list_tasks(project)
        if not tasks:
            self._reply("No tasks found.")
            return
        lines = [f"\U0001f4cb Tasks{f' for {project}' if project else ''}:"]
        for t in tasks[:20]:  # cap at 20 to avoid flooding
            lines.append(f"  [{t.state}] {t.project}/{t.slug}")
        if len(tasks) > 20:
            lines.append(f"  … and {len(tasks) - 20} more")
        self._reply("\n".join(lines))

    def _cmd_start(self, args: str) -> None:
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            self._reply("Usage: /start <project> <slug>")
            return
        project, slug = parts
        task = self._store.get_task(project, slug)
        if task is None:
            self._reply(f"Task not found: {project}/{slug}")
            return
        if task.state != TaskState.PENDING:
            self._reply(f"Task '{slug}' is in state {task.state} — only PENDING tasks can be force-started.")
            return
        self._reply(f"\u25b6\ufe0f Task '{slug}' will be dispatched on the next scheduler poll.")

    def _cmd_cancel(self, args: str) -> None:
        slug = args.strip()
        if not slug:
            self._reply("Usage: /cancel <slug>")
            return
        # Find across all projects
        tasks = [t for t in self._store.list_tasks() if t.slug == slug]
        if not tasks:
            self._reply(f"No task with slug '{slug}' found.")
            return
        for task in tasks:
            ok = self._store.cancel_task(task.project, slug)
            if ok:
                self._reply(f"\u274c Cancelled: {task.project}/{slug}")
            else:
                self._reply(f"Could not cancel {task.project}/{slug} (terminal state).")

    def _cmd_approve(self, _args: str) -> None:
        with self._approval_lock:
            if not self._pending_approval:
                self._reply("No pending approval.")
                return
            for slug in self._pending_approval:
                self._pending_approval[slug] = ApprovalResult(approved=True, reason=None)
        self._reply("\u2705 Approved.")

    def _cmd_reject(self, args: str) -> None:
        reason = args.strip() or None
        with self._approval_lock:
            if not self._pending_approval:
                self._reply("No pending approval.")
                return
            for slug in self._pending_approval:
                self._pending_approval[slug] = ApprovalResult(approved=False, reason=reason)
        self._reply(f"\u274c Rejected. Reason: {reason or 'none given'}")

    def _cmd_hint(self, args: str) -> None:
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            self._reply("Usage: /hint <slug> <text>")
            return
        slug, hint = parts
        tasks = [t for t in self._store.list_tasks() if t.slug == slug]
        if not tasks:
            self._reply(f"No task with slug '{slug}'.")
            return
        task = tasks[0]
        current_guide = task.explore_guide or ""
        new_guide = f"{current_guide}\n## Human hint\n{hint}".strip()
        # Update explore_guide directly — use accumulate pattern
        self._store._conn.execute(
            "UPDATE tasks SET explore_guide = ?, updated_at = ? WHERE project = ? AND slug = ?",
            (new_guide, _now(), task.project, slug),
        )
        self._store._conn.commit()
        self._reply(f"\U0001f4dd Hint added to '{slug}' guide.")

    def _cmd_session(self, args: str) -> None:
        slug = args.strip()
        if not slug:
            self._reply("Usage: /session <slug>")
            return
        tasks = [t for t in self._store.list_tasks() if t.slug == slug]
        if not tasks:
            self._reply(f"No task with slug '{slug}'.")
            return
        task = tasks[0]
        repo_path = _get_repo_path(self._registry_path, task.project)
        if repo_path is None:
            self._reply(f"Project '{task.project}' not found in registry.")
            return
        try:
            session_name = spawn_handoff_session(repo_path, slug, self._handoff_shell)
            self._reply(
                f"\U0001f5a5\ufe0f Session ready: {session_name}\n"
                f"Attach with:\n  tmux attach -t {session_name}\n"
                "(SSH to server first if remote)"
            )
        except subprocess.CalledProcessError:
            self._reply(f"Failed to create tmux session for '{slug}'. Check server logs.")

    def _cmd_kill(self, _args: str) -> None:
        self._reply("\U0001f6d1 Sending SIGTERM to orchestrator...")
        os.kill(os.getpid(), 2)  # SIGINT to main process (SIGTERM handled by Scheduler)

    def _cmd_unknown(self, _args: str) -> None:
        self._reply(
            "Unknown command. Available:\n"
            "/status /list /start /cancel /approve /reject /hint /session /kill"
        )

    # ------------------------------------------------------------------
    # Approval coordination (called by scheduler)
    # ------------------------------------------------------------------

    def request_approval(self, slug: str, plan_text: str) -> None:
        """Register a pending approval and send the plan. Non-blocking."""
        with self._approval_lock:
            self._pending_approval[slug] = None
        send_message(self._token, self._chat_id, plan_text)
        send_message(self._token, self._chat_id,
                     "Reply /approve to proceed or /reject <reason> to abort.")

    def poll_approval(self, slug: str) -> ApprovalResult | None:
        """Return the ApprovalResult if received, None if still waiting."""
        with self._approval_lock:
            return self._pending_approval.get(slug)

    def clear_approval(self, slug: str) -> None:
        with self._approval_lock:
            self._pending_approval.pop(slug, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _get_repo_path(registry_path: Path, project_name: str) -> Path | None:
    """Look up a project's repo_path from the registry file."""
    import json
    try:
        data = json.loads(registry_path.read_text())
        for entry in data:
            if entry.get("name") == project_name:
                return Path(entry["repo_path"])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Scheduler integration helpers (backward compat)
# ---------------------------------------------------------------------------

def make_notify_stuck(token: str, chat_id: str) -> Callable[[str, int, str], None]:
    """Return a notify_stuck callable bound to token and chat_id."""
    def _notify(task_slug: str, retry_n: int, branch_name: str) -> None:
        notify_stuck(token, chat_id, task_slug, retry_n, branch_name)
    return _notify
