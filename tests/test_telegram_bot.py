import time
from unittest.mock import MagicMock, call, patch

import httpx
import pytest

from orchestrator.telegram_bot import (
    ApprovalResult,
    get_updates,
    make_notify_stuck,
    notify_stuck,
    notify_uat_ready,
    send_message,
    wait_for_plan_approval,
)


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


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------

def test_send_message_posts_correct_payload():
    resp = _mock_response(200)
    with patch("orchestrator.telegram_bot.httpx.post", return_value=resp) as mock_post:
        send_message("tok123", "chat456", "hello world")

    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert "bot123" not in mock_post.call_args[0][0]  # token in URL
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
# wait_for_plan_approval
# ---------------------------------------------------------------------------

def test_approval_approve():
    updates = [_update(1, "111111", "/approve")]
    with patch("orchestrator.telegram_bot.send_message") as mock_send, \
         patch("orchestrator.telegram_bot.get_updates", return_value=updates):
        result = wait_for_plan_approval("tok", "111111", "plan text", timeout_seconds=60)

    assert result == ApprovalResult(approved=True, reason=None)
    assert mock_send.call_count == 2  # plan text + instructions


def test_approval_reject_with_reason():
    updates = [_update(1, "111111", "/reject the API design is wrong")]
    with patch("orchestrator.telegram_bot.send_message"), \
         patch("orchestrator.telegram_bot.get_updates", return_value=updates):
        result = wait_for_plan_approval("tok", "111111", "plan", timeout_seconds=60)

    assert result == ApprovalResult(approved=False, reason="the API design is wrong")


def test_approval_reject_no_reason():
    updates = [_update(1, "111111", "/reject")]
    with patch("orchestrator.telegram_bot.send_message"), \
         patch("orchestrator.telegram_bot.get_updates", return_value=updates):
        result = wait_for_plan_approval("tok", "111111", "plan", timeout_seconds=60)

    assert result == ApprovalResult(approved=False, reason=None)


def test_approval_ignores_wrong_chat():
    # First update from wrong chat, second from correct chat
    call_count = 0

    def fake_updates(token, offset=0, timeout=30):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [_update(1, "999999", "/approve")]
        return [_update(2, "111111", "/approve")]

    with patch("orchestrator.telegram_bot.send_message"), \
         patch("orchestrator.telegram_bot.get_updates", side_effect=fake_updates):
        result = wait_for_plan_approval("tok", "111111", "plan", timeout_seconds=60)

    assert result.approved is True
    assert call_count == 2


def test_approval_advances_offset():
    """Offset increments so already-seen updates aren't re-processed."""
    offsets_used = []

    def fake_updates(token, offset=0, timeout=30):
        offsets_used.append(offset)
        if offset == 0:
            return [_update(10, "111111", "random message")]
        return [_update(11, "111111", "/approve")]

    with patch("orchestrator.telegram_bot.send_message"), \
         patch("orchestrator.telegram_bot.get_updates", side_effect=fake_updates):
        result = wait_for_plan_approval("tok", "111111", "plan", timeout_seconds=60)

    assert result.approved is True
    assert offsets_used[0] == 0
    assert offsets_used[1] == 11  # update_id 10 + 1


def test_approval_timeout():
    with patch("orchestrator.telegram_bot.send_message"), \
         patch("orchestrator.telegram_bot.get_updates", return_value=[]), \
         patch("orchestrator.telegram_bot.time.monotonic", side_effect=[0.0, 999.0, 999.0]):
        result = wait_for_plan_approval("tok", "111111", "plan", timeout_seconds=1)

    assert result == ApprovalResult(approved=False, reason="timeout")


# ---------------------------------------------------------------------------
# notify_stuck
# ---------------------------------------------------------------------------

def test_notify_stuck_sends_message():
    with patch("orchestrator.telegram_bot.send_message") as mock_send:
        notify_stuck("tok", "111111", "my-task", 2, "feature/my-task")

    mock_send.assert_called_once()
    _, kwargs = mock_send.call_args
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
