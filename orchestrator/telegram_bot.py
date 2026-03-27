"""
Telegram notification and interaction via raw httpx. No wrapper library.
Phase 4.

All functions are stateless — token and chat_id passed explicitly.
Use make_notify_stuck() to get a Callable[[str, int, str], None] compatible
with Scheduler's notify_stuck parameter.
"""

import time
from typing import Callable, NamedTuple

import httpx

_API = "https://api.telegram.org/bot{token}/{method}"


# ---------------------------------------------------------------------------
# Low-level primitives
# ---------------------------------------------------------------------------

def send_message(token: str, chat_id: str, text: str) -> None:
    """POST a text message to a Telegram chat. Raises on non-2xx."""
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
# Plan approval
# ---------------------------------------------------------------------------

class ApprovalResult(NamedTuple):
    approved: bool
    reason: str | None  # rejection reason or "timeout"


def wait_for_plan_approval(
    token: str,
    chat_id: str,
    plan_text: str,
    timeout_seconds: int = 86400,
) -> ApprovalResult:
    """Send the plan, then poll until the user replies /approve or /reject <reason>.

    Returns ApprovalResult(approved=False, reason="timeout") if no reply arrives
    within timeout_seconds.
    """
    send_message(token, chat_id, plan_text)
    send_message(token, chat_id, "Reply /approve to proceed or /reject <reason> to abort.")

    deadline = time.monotonic() + timeout_seconds
    offset = 0

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        poll_timeout = min(30, max(1, int(remaining)))

        updates = get_updates(token, offset=offset, timeout=poll_timeout)
        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            if str(msg.get("chat", {}).get("id", "")) != str(chat_id):
                continue
            text = msg.get("text", "").strip()
            if text == "/approve":
                return ApprovalResult(approved=True, reason=None)
            if text.startswith("/reject"):
                reason = text[len("/reject"):].strip() or None
                return ApprovalResult(approved=False, reason=reason)

    return ApprovalResult(approved=False, reason="timeout")


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
        f"\u26a0\ufe0f STUCK: {task_slug} (attempt {retry_n})\n"
        f"Branch: {branch_name}\n"
        "Reply with a hint for the next session, or do nothing to let retry proceed automatically."
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
# Scheduler integration
# ---------------------------------------------------------------------------

def make_notify_stuck(token: str, chat_id: str) -> Callable[[str, int, str], None]:
    """Return a notify_stuck callable bound to token and chat_id.

    Pass the result directly as Scheduler's notify_stuck parameter.
    """
    def _notify(task_slug: str, retry_n: int, branch_name: str) -> None:
        notify_stuck(token, chat_id, task_slug, retry_n, branch_name)
    return _notify
