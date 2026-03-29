from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from orchestrator.telegram_bot import (
    ApprovalResult,
    TelegramCommandDispatcher,
    get_updates,
    make_notify_stuck,
    notify_stuck,
    notify_uat_ready,
    send_message,
)
from orchestrator.state_store import StateStore, TaskState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(status=200, json_data=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = json_data or {}
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


def _update(update_id: int, chat_id: str | int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": int(chat_id)},
            "text": text,
        },
    }


@pytest.fixture
def store():
    s = StateStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def registry_path(tmp_path):
    import json
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps([{
        "name": "proj-a",
        "repo_path": str(tmp_path / "proj-a"),
        "github_url": "https://github.com/user/proj-a",
    }]))
    return reg


def make_dispatcher(store, registry_path):
    return TelegramCommandDispatcher(
        token="tok",
        chat_id="111111",
        state_store=store,
        registry_path=registry_path,
    )


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------

def test_send_message_posts_correct_payload():
    resp = _mock_response(200)
    with patch("orchestrator.telegram_bot.httpx.post", return_value=resp) as mock_post:
        send_message("tok123", "chat456", "hello world")

    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert "bottok123" in mock_post.call_args[0][0]
    assert kwargs["json"]["chat_id"] == "chat456"
    assert kwargs["json"]["text"] == "hello world"


def test_send_message_raises_on_non_2xx():
    resp = _mock_response(400)
    with patch("orchestrator.telegram_bot.httpx.post", return_value=resp):
        with pytest.raises(httpx.HTTPStatusError):
            send_message("tok", "chat", "msg")


def test_send_message_url_includes_token():
    resp = _mock_response(200)
    with patch("orchestrator.telegram_bot.httpx.post", return_value=resp) as mock_post:
        send_message("mytoken", "chat", "text")
    url = mock_post.call_args[0][0]
    assert "botmytoken" in url
    assert "sendMessage" in url


# ---------------------------------------------------------------------------
# get_updates
# ---------------------------------------------------------------------------

def test_get_updates_returns_result_list():
    updates = [{"update_id": 1, "message": {}}]
    resp = _mock_response(200, json_data={"ok": True, "result": updates})
    with patch("orchestrator.telegram_bot.httpx.get", return_value=resp):
        result = get_updates("tok", offset=5, timeout=10)
    assert result == updates


def test_get_updates_passes_offset_and_timeout():
    resp = _mock_response(200, json_data={"result": []})
    with patch("orchestrator.telegram_bot.httpx.get", return_value=resp) as mock_get:
        get_updates("tok", offset=42, timeout=15)
    params = mock_get.call_args.kwargs["params"]
    assert params["offset"] == 42
    assert params["timeout"] == 15


def test_get_updates_empty_result_on_missing_key():
    resp = _mock_response(200, json_data={"ok": True})
    with patch("orchestrator.telegram_bot.httpx.get", return_value=resp):
        result = get_updates("tok")
    assert result == []


def test_get_updates_raises_on_non_2xx():
    resp = _mock_response(500)
    with patch("orchestrator.telegram_bot.httpx.get", return_value=resp):
        with pytest.raises(httpx.HTTPStatusError):
            get_updates("tok")


# ---------------------------------------------------------------------------
# notify_stuck
# ---------------------------------------------------------------------------

def test_notify_stuck_sends_message():
    with patch("orchestrator.telegram_bot.send_message") as mock_send:
        notify_stuck("tok", "111111", "my-task", 2, "feature/my-task")
    mock_send.assert_called_once()
    text = mock_send.call_args[0][2]
    assert "my-task" in text
    assert "2" in text
    assert "feature/my-task" in text


# ---------------------------------------------------------------------------
# notify_uat_ready
# ---------------------------------------------------------------------------

def test_notify_uat_ready_sends_message():
    with patch("orchestrator.telegram_bot.send_message") as mock_send:
        notify_uat_ready("tok", "111111", "proj-a", "https://staging.run.app", "Added rate limiting.")
    text = mock_send.call_args[0][2]
    assert "proj-a" in text
    assert "https://staging.run.app" in text
    assert "Added rate limiting." in text


# ---------------------------------------------------------------------------
# make_notify_stuck
# ---------------------------------------------------------------------------

def test_make_notify_stuck_returns_callable():
    fn = make_notify_stuck("tok", "111111")
    assert callable(fn)


def test_make_notify_stuck_binds_token_and_chat():
    with patch("orchestrator.telegram_bot.notify_stuck") as mock_notify:
        fn = make_notify_stuck("tok", "111111")
        fn("my-task", 1, "feature/my-task")
    mock_notify.assert_called_once_with("tok", "111111", "my-task", 1, "feature/my-task")


# ---------------------------------------------------------------------------
# TelegramCommandDispatcher — command dispatch
# ---------------------------------------------------------------------------

def test_dispatcher_ignores_wrong_chat(store, registry_path):
    d = make_dispatcher(store, registry_path)
    with patch.object(d, "_reply") as mock_reply:
        d._handle_update(_update(1, "999999", "/status"))
    mock_reply.assert_not_called()


def test_dispatcher_cmd_status_empty(store, registry_path):
    d = make_dispatcher(store, registry_path)
    replies = []
    with patch.object(d, "_reply", side_effect=replies.append):
        d._handle_update(_update(1, "111111", "/status"))
    assert len(replies) == 1
    assert "Total tasks: 0" in replies[0]


def test_dispatcher_cmd_status_with_tasks(store, registry_path):
    store.add_task("proj-a", "fix-bug", "/path.md")
    store.add_task("proj-a", "add-feat", "/path2.md")
    d = make_dispatcher(store, registry_path)
    replies = []
    with patch.object(d, "_reply", side_effect=replies.append):
        d._handle_update(_update(1, "111111", "/status"))
    assert "PENDING: 2" in replies[0]


def test_dispatcher_cmd_list_all(store, registry_path):
    store.add_task("proj-a", "fix-bug", "/path.md")
    d = make_dispatcher(store, registry_path)
    replies = []
    with patch.object(d, "_reply", side_effect=replies.append):
        d._handle_update(_update(1, "111111", "/list"))
    assert "fix-bug" in replies[0]


def test_dispatcher_cmd_cancel_pending(store, registry_path):
    store.add_task("proj-a", "fix-bug", "/path.md")
    d = make_dispatcher(store, registry_path)
    with patch.object(d, "_reply"):
        d._handle_update(_update(1, "111111", "/cancel fix-bug"))
    task = store.get_task("proj-a", "fix-bug")
    assert task.state == TaskState.CANCELLED


def test_dispatcher_cmd_cancel_missing(store, registry_path):
    d = make_dispatcher(store, registry_path)
    replies = []
    with patch.object(d, "_reply", side_effect=replies.append):
        d._handle_update(_update(1, "111111", "/cancel nonexistent"))
    assert "not found" in replies[0].lower() or "No task" in replies[0]


def test_dispatcher_cmd_approve(store, registry_path):
    d = make_dispatcher(store, registry_path)
    # Register a pending approval
    store.add_task("proj-a", "fix-bug", "/path.md")
    d._pending_approval["fix-bug"] = None
    with patch.object(d, "_reply"):
        d._handle_update(_update(1, "111111", "/approve"))
    result = d.poll_approval("fix-bug")
    assert result is not None
    assert result.approved is True


def test_dispatcher_cmd_reject_with_reason(store, registry_path):
    d = make_dispatcher(store, registry_path)
    d._pending_approval["fix-bug"] = None
    with patch.object(d, "_reply"):
        d._handle_update(_update(1, "111111", "/reject the design is wrong"))
    result = d.poll_approval("fix-bug")
    assert result.approved is False
    assert result.reason == "the design is wrong"


def test_dispatcher_cmd_hint_appends_guide(store, registry_path):
    store.add_task("proj-a", "fix-bug", "/path.md")
    d = make_dispatcher(store, registry_path)
    with patch.object(d, "_reply"):
        d._handle_update(_update(1, "111111", "/hint fix-bug try checking the null pointer at line 42"))
    task = store.get_task("proj-a", "fix-bug")
    assert "null pointer at line 42" in (task.explore_guide or "")


def test_dispatcher_cmd_unknown(store, registry_path):
    d = make_dispatcher(store, registry_path)
    replies = []
    with patch.object(d, "_reply", side_effect=replies.append):
        d._handle_update(_update(1, "111111", "/notacommand"))
    assert "Unknown command" in replies[0]


def test_dispatcher_approval_no_pending(store, registry_path):
    d = make_dispatcher(store, registry_path)
    replies = []
    with patch.object(d, "_reply", side_effect=replies.append):
        d._handle_update(_update(1, "111111", "/approve"))
    assert "No pending approval" in replies[0]


# ---------------------------------------------------------------------------
# TelegramCommandDispatcher — request_approval / poll_approval
# ---------------------------------------------------------------------------

def test_request_approval_sets_pending(store, registry_path):
    d = make_dispatcher(store, registry_path)
    with patch("orchestrator.telegram_bot.send_message"):
        d.request_approval("fix-bug", "Here is the plan.")
    assert "fix-bug" in d._pending_approval
    assert d._pending_approval["fix-bug"] is None  # waiting


def test_clear_approval_removes_entry(store, registry_path):
    d = make_dispatcher(store, registry_path)
    d._pending_approval["fix-bug"] = ApprovalResult(approved=True, reason=None)
    d.clear_approval("fix-bug")
    assert "fix-bug" not in d._pending_approval
