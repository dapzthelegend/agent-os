"""
Tests for notification_router: Discord Bot DM → Gmail → stderr chain.

All HTTP calls are mocked; no real network requests are made.
"""
from __future__ import annotations

import json
import sys
from io import StringIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from src.agentic_os.models import ApprovalRecord, TaskRecord
from src.agentic_os import notification_router as nr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _task(*, id: str = "task_abc", status: str = "awaiting_approval") -> TaskRecord:
    return TaskRecord(
        id=id,
        created_at="2026-03-23T10:00:00Z",
        updated_at="2026-03-23T10:00:00Z",
        domain="personal",
        intent_type="draft",
        risk_level="medium",
        status=status,
        approval_state="pending",
        user_request="Review contract for supplier XYZ\nMore details here",
        result_summary="Draft reviewed",
        artifact_ref=None,
        external_ref="gmail:msg-001",
        target="gmail_reply_draft",
        request_metadata_json=None,
        operation_key="gmail:msg-001",
        external_write=False,
        policy_decision="approval_required",
        action_source="openclaw_tool",
        retry_count=0,
    )


def _approval(*, id: str = "appr_xyz") -> ApprovalRecord:
    return ApprovalRecord(
        id=id,
        task_id="task_abc",
        status="pending",
        subject_type="artifact",
        artifact_id=None,
        action_target=None,
        operation_key=None,
        payload_json="{}",
        decision_note=None,
        created_at="2026-03-23T10:00:00Z",
        updated_at="2026-03-23T10:00:00Z",
        decided_at=None,
    )


def _mock_urlopen_discord(channel_id: str = "dm_channel_99", msg_status: int = 200):
    """Return a context-manager mock that simulates Discord API responses."""
    dm_response = MagicMock()
    dm_response.read.return_value = json.dumps({"id": channel_id}).encode()
    dm_response.status = 200
    dm_response.__enter__ = lambda s: s
    dm_response.__exit__ = MagicMock(return_value=False)

    msg_response = MagicMock()
    msg_response.read.return_value = b"{}"
    msg_response.status = msg_status
    msg_response.__enter__ = lambda s: s
    msg_response.__exit__ = MagicMock(return_value=False)

    return [dm_response, msg_response]


# ---------------------------------------------------------------------------
# Discord DM delivery
# ---------------------------------------------------------------------------

class TestDiscordDmDelivery:
    def test_route_task_completed_via_discord(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok_test")
        monkeypatch.setenv("DISCORD_USER_ID", "user_111")

        responses = _mock_urlopen_discord()
        with patch("src.agentic_os.notification_router.urllib_request.urlopen",
                   side_effect=responses):
            result = nr.route_task_completed(_task())

        assert result.channel == "discord"
        assert result.success is True

    def test_route_task_failed_via_discord(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok_test")
        monkeypatch.setenv("DISCORD_USER_ID", "user_111")

        responses = _mock_urlopen_discord()
        with patch("src.agentic_os.notification_router.urllib_request.urlopen",
                   side_effect=responses):
            result = nr.route_task_failed(_task())

        assert result.channel == "discord"
        assert result.success is True

    def test_route_approval_requested_via_discord(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok_test")
        monkeypatch.setenv("DISCORD_USER_ID", "user_111")

        responses = _mock_urlopen_discord()
        with patch("src.agentic_os.notification_router.urllib_request.urlopen",
                   side_effect=responses):
            result = nr.route_approval_requested(_task(), _approval())

        assert result.channel == "discord"
        assert result.success is True
        # message should contain the approval id
        assert "appr_xyz" in result.message

    def test_route_overdue_task_via_discord(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok_test")
        monkeypatch.setenv("DISCORD_USER_ID", "user_111")

        responses = _mock_urlopen_discord()
        with patch("src.agentic_os.notification_router.urllib_request.urlopen",
                   side_effect=responses):
            result = nr.route_overdue_task(_task(), hours_overdue=52.5)

        assert result.channel == "discord"
        assert result.success is True
        assert "52h" in result.message

    def test_route_approval_reminder_via_discord(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok_test")
        monkeypatch.setenv("DISCORD_USER_ID", "user_111")

        responses = _mock_urlopen_discord()
        with patch("src.agentic_os.notification_router.urllib_request.urlopen",
                   side_effect=responses):
            result = nr.route_approval_reminder(_task(), _approval(), hours_pending=2.0)

        assert result.channel == "discord"
        assert result.success is True
        assert "2h" in result.message

    def test_discord_dm_sends_to_correct_user(self, monkeypatch):
        """Verify recipient_id is passed in the DM channel creation request."""
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok_abc")
        monkeypatch.setenv("DISCORD_USER_ID", "user_999")

        captured_requests = []
        responses = _mock_urlopen_discord(channel_id="dm_999")

        def fake_urlopen(req, timeout=None):
            captured_requests.append(req)
            return responses.pop(0)

        with patch("src.agentic_os.notification_router.urllib_request.urlopen",
                   side_effect=fake_urlopen):
            nr.route_task_completed(_task())

        dm_req = captured_requests[0]
        body = json.loads(dm_req.data.decode())
        assert body["recipient_id"] == "user_999"
        assert "Bot tok_abc" in dm_req.get_header("Authorization")

    def test_message_sent_to_dm_channel(self, monkeypatch):
        """Verify message is posted to the channel returned by DM creation."""
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok_abc")
        monkeypatch.setenv("DISCORD_USER_ID", "user_999")

        captured_requests = []
        responses = _mock_urlopen_discord(channel_id="chan_42")

        def fake_urlopen(req, timeout=None):
            captured_requests.append(req)
            return responses.pop(0)

        with patch("src.agentic_os.notification_router.urllib_request.urlopen",
                   side_effect=fake_urlopen):
            nr.route_task_completed(_task())

        msg_req = captured_requests[1]
        assert "chan_42" in msg_req.full_url
        body = json.loads(msg_req.data.decode())
        assert "Task completed" in body["content"]


# ---------------------------------------------------------------------------
# Gmail fallback
# ---------------------------------------------------------------------------

class TestGmailFallback:
    def test_falls_back_to_gmail_when_discord_unavailable(self, monkeypatch):
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("DISCORD_USER_ID", raising=False)

        with patch("src.agentic_os.gmail_sender.send_email", return_value=True) as mock_send:
            result = nr.route_task_completed(_task())

        assert result.channel == "gmail"
        assert result.success is True
        mock_send.assert_called_once()
        _, kwargs = mock_send.call_args
        assert kwargs["to"] == "franchieinc@gmail.com"

    def test_gmail_fallback_when_discord_dm_fails(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok_bad")
        monkeypatch.setenv("DISCORD_USER_ID", "user_111")

        from urllib.error import URLError

        with patch("src.agentic_os.notification_router.urllib_request.urlopen",
                   side_effect=URLError("connection refused")):
            with patch("src.agentic_os.gmail_sender.send_email", return_value=True) as mock_send:
                result = nr.route_task_failed(_task())

        assert result.channel == "gmail"
        assert result.success is True
        mock_send.assert_called_once()

    def test_approval_email_subject_contains_task_title(self, monkeypatch):
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("DISCORD_USER_ID", raising=False)

        with patch("src.agentic_os.gmail_sender.send_email", return_value=True) as mock_send:
            nr.route_approval_requested(_task(), _approval())

        _, kwargs = mock_send.call_args
        assert "Approval required" in kwargs["subject"]
        assert "Review contract" in kwargs["subject"]

    def test_gmail_body_strips_discord_markdown(self, monkeypatch):
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("DISCORD_USER_ID", raising=False)

        with patch("src.agentic_os.gmail_sender.send_email", return_value=True) as mock_send:
            nr.route_task_completed(_task())

        _, kwargs = mock_send.call_args
        body = kwargs["body"]
        assert "**" not in body
        assert "`" not in body


# ---------------------------------------------------------------------------
# Stderr last-resort
# ---------------------------------------------------------------------------

class TestStderrFallback:
    def test_falls_back_to_stderr_when_all_fail(self, monkeypatch, capsys):
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("DISCORD_USER_ID", raising=False)

        with patch("src.agentic_os.gmail_sender.send_email", return_value=False):
            result = nr.route_task_completed(_task())

        assert result.channel == "stderr"
        assert result.success is False
        assert result.error == "all_channels_failed"

    def test_stderr_when_gmail_raises(self, monkeypatch, capsys):
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("DISCORD_USER_ID", raising=False)

        with patch("src.agentic_os.gmail_sender.send_email",
                   side_effect=RuntimeError("credentials not found")):
            result = nr.route_task_completed(_task())

        assert result.channel == "stderr"
        captured = capsys.readouterr()
        assert "credentials not found" in captured.err


# ---------------------------------------------------------------------------
# _strip_discord_markdown
# ---------------------------------------------------------------------------

class TestStripMarkdown:
    def test_removes_bold(self):
        assert nr._strip_discord_markdown("**hello**") == "hello"

    def test_removes_backticks(self):
        assert nr._strip_discord_markdown("`task_abc`") == "task_abc"

    def test_preserves_plain_text(self):
        assert nr._strip_discord_markdown("hello world") == "hello world"


# ---------------------------------------------------------------------------
# _task_title truncation
# ---------------------------------------------------------------------------

class TestTaskTitle:
    def test_uses_first_line(self):
        task = _task()
        assert nr._task_title(task) == "Review contract for supplier XYZ"

    def test_truncates_to_100_chars(self):
        long_req = "A" * 150
        task = TaskRecord(
            id="t1", created_at="", updated_at="",
            domain="personal", intent_type="read", risk_level="low",
            status="new", approval_state="not_needed",
            user_request=long_req, result_summary=None,
            artifact_ref=None, external_ref=None, target=None,
            request_metadata_json=None, operation_key=None,
            external_write=False, policy_decision=None,
            action_source="openclaw_tool",
        )
        assert len(nr._task_title(task)) == 100


# ---------------------------------------------------------------------------
# notify_overdue_tasks bulk helper
# ---------------------------------------------------------------------------

class TestNotifyOverdueTasks:
    def test_sends_one_alert_per_task(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok_test")
        monkeypatch.setenv("DISCORD_USER_ID", "user_111")

        tasks = [_task(id=f"t{i}") for i in range(3)]

        responses = []
        for _ in tasks:
            responses.extend(_mock_urlopen_discord())

        with patch("src.agentic_os.notification_router.urllib_request.urlopen",
                   side_effect=responses):
            results = nr.notify_overdue_tasks(tasks)

        assert len(results) == 3
        assert all(r.channel == "discord" for r in results)
