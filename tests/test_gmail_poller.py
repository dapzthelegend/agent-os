"""
Tests for gmail_poller: inbox fetch, classification, multi-account merge.

All HTTP and credential calls are mocked — no real network.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.agentic_os import gmail_poller as gp


# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------

def _creds_file(tmp_path: Path, name: str = "creds.json") -> str:
    data = {"web": {"client_id": "cid", "client_secret": "csec", "refresh_token": "rtok"}}
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return str(p)


def _stub(id: str) -> dict:
    return {"id": id, "threadId": f"thread_{id}"}


def _message_response(
    id: str,
    subject: str = "Hello",
    from_: str = "Alice <alice@example.com>",
    snippet: str = "Some body text",
    has_list_unsubscribe: bool = False,
) -> dict:
    headers = [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": from_},
        {"name": "Date", "value": "Mon, 23 Mar 2026 08:00:00 +0000"},
    ]
    if has_list_unsubscribe:
        headers.append({"name": "List-Unsubscribe", "value": "<mailto:unsub@list.example.com>"})
    return {
        "id": id,
        "snippet": snippet,
        "payload": {"headers": headers},
    }


def _patch_gmail_get(list_response: dict, messages: list[dict]):
    """Patch _gmail_get to return list_response first, then each message dict in order."""
    responses = [list_response] + messages

    call_count = 0

    def fake_gmail_get(url: str, token: str) -> dict:
        nonlocal call_count
        resp = responses[call_count] if call_count < len(responses) else {}
        call_count += 1
        return resp

    return patch("src.agentic_os.gmail_poller._gmail_get", side_effect=fake_gmail_get)


def _patch_auth(token: str = "test_token"):
    return patch(
        "src.agentic_os.gmail_poller._refresh_access_token",
        return_value=token,
    )


def _patch_load_creds():
    return patch(
        "src.agentic_os.gmail_poller._load_credentials",
        return_value={"client_id": "cid", "client_secret": "csec", "refresh_token": "rtok"},
    )


# ---------------------------------------------------------------------------
# _parse_from
# ---------------------------------------------------------------------------

class TestParseFrom:
    def test_bracketed_email(self):
        name, email = gp._parse_from("Alice Smith <alice@example.com>")
        assert email == "alice@example.com"
        assert name == "Alice Smith"

    def test_bare_email(self):
        name, email = gp._parse_from("alice@example.com")
        assert email == "alice@example.com"

    def test_quoted_name(self):
        _, email = gp._parse_from('"No Reply" <noreply@service.com>')
        assert email == "noreply@service.com"


# ---------------------------------------------------------------------------
# _is_automated_sender
# ---------------------------------------------------------------------------

class TestIsAutomatedSender:
    def test_noreply(self):
        assert gp._is_automated_sender("noreply@example.com") is True

    def test_no_reply_hyphen(self):
        assert gp._is_automated_sender("no-reply@service.com") is True

    def test_notifications(self):
        assert gp._is_automated_sender("notifications@github.com") is True

    def test_real_person(self):
        assert gp._is_automated_sender("alice@example.com") is False

    def test_newsletters(self):
        assert gp._is_automated_sender("newsletter@company.com") is True


# ---------------------------------------------------------------------------
# _has_urgent_keywords
# ---------------------------------------------------------------------------

class TestHasUrgentKeywords:
    def test_urgent_in_subject(self):
        assert gp._has_urgent_keywords("URGENT: Please review") is True

    def test_asap(self):
        assert gp._has_urgent_keywords("Please reply ASAP") is True

    def test_deadline(self):
        assert gp._has_urgent_keywords("Deadline is tomorrow") is True

    def test_normal_subject(self):
        assert gp._has_urgent_keywords("Meeting notes from Monday") is False


# ---------------------------------------------------------------------------
# _classify_agent_inbox
# ---------------------------------------------------------------------------

class TestClassifyAgentInbox:
    def _email(self, subject, from_, is_automated=False, is_urgent=False, snippet=""):
        _, sender_email = gp._parse_from(from_)
        return {
            "id": "m1",
            "subject": subject,
            "sender": from_,
            "sender_email": sender_email,
            "sender_name": "",
            "date": "",
            "snippet": snippet,
            "is_automated": is_automated,
            "is_urgent": is_urgent,
        }

    def test_automated_sender_goes_to_alerts(self):
        email = self._email("Receipt #123", "noreply@shop.com", is_automated=True)
        result = gp._classify_agent_inbox([email])
        assert len(result["alerts"]) == 1
        assert len(result["operational_items"]) == 0

    def test_operational_keyword_goes_to_operational(self):
        email = self._email("Invoice #5 from supplier", "supplier@company.com")
        result = gp._classify_agent_inbox([email])
        assert len(result["operational_items"]) == 1
        assert result["operational_items"][0]["actionable"] is True

    def test_urgent_goes_to_operational_and_actionable(self):
        email = self._email("URGENT: Contract deadline", "lawyer@firm.com", is_urgent=True)
        result = gp._classify_agent_inbox([email])
        assert len(result["operational_items"]) == 1
        assert result["operational_items"][0]["actionable"] is True

    def test_real_person_non_keyword_goes_to_operational(self):
        email = self._email("Following up on our call", "partner@example.com")
        result = gp._classify_agent_inbox([email])
        assert len(result["operational_items"]) == 1

    def test_alert_keyword_goes_to_alerts(self):
        email = self._email("System alert: disk usage high", "monitoring@infra.com", is_automated=True)
        result = gp._classify_agent_inbox([email])
        assert len(result["alerts"]) == 1


# ---------------------------------------------------------------------------
# _classify_personal_inbox
# ---------------------------------------------------------------------------

class TestClassifyPersonalInbox:
    def _email(self, subject, from_, is_automated=False, is_urgent=False, snippet=""):
        _, sender_email = gp._parse_from(from_)
        return {
            "id": "m1",
            "subject": subject,
            "sender": from_,
            "sender_email": sender_email,
            "sender_name": "",
            "date": "",
            "snippet": snippet,
            "is_automated": is_automated,
            "is_urgent": is_urgent,
        }

    def test_urgent_email(self):
        email = self._email("URGENT: Family matter", "mum@example.com", is_urgent=True)
        result = gp._classify_personal_inbox([email])
        assert len(result["urgent"]) == 1
        assert result["urgent"][0]["actionable"] is True

    def test_real_person_goes_to_needs_reply(self):
        email = self._email("Dinner this Friday?", "friend@example.com")
        result = gp._classify_personal_inbox([email])
        assert len(result["needs_reply"]) == 1
        assert result["needs_reply"][0]["actionable"] is True

    def test_automated_goes_to_important_fyi(self):
        email = self._email("Your monthly statement", "noreply@bank.com", is_automated=True)
        result = gp._classify_personal_inbox([email])
        assert len(result["important_fyi"]) == 1


# ---------------------------------------------------------------------------
# poll_agent_inbox — full integration with mocked HTTP
# ---------------------------------------------------------------------------

class TestPollAgentInbox:
    def test_returns_empty_when_no_creds_env(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GOOGLE_CREDENTIALS_PATH", raising=False)
        result = gp.poll_agent_inbox(credentials_path=None)
        assert result == gp._empty_inbox_summary()

    def test_classifies_operational_email(self, tmp_path):
        list_resp = {"messages": [_stub("m1")]}
        messages = [_message_response("m1", subject="Invoice from supplier", from_="billing@vendor.com")]

        with _patch_load_creds(), _patch_auth(), _patch_gmail_get(list_resp, messages):
            result = gp.poll_agent_inbox(credentials_path="/fake/creds.json")

        assert len(result["operational_items"]) == 1
        assert result["operational_items"][0]["subject"] == "Invoice from supplier"

    def test_classifies_automated_alert(self, tmp_path):
        list_resp = {"messages": [_stub("m2")]}
        messages = [_message_response("m2", subject="System alert", from_="alerts@monitoring.com")]

        with _patch_load_creds(), _patch_auth(), _patch_gmail_get(list_resp, messages):
            result = gp.poll_agent_inbox(credentials_path="/fake/creds.json")

        assert len(result["alerts"]) == 1

    def test_handles_empty_message_list(self, tmp_path):
        list_resp = {"messages": []}
        with _patch_load_creds(), _patch_auth(), _patch_gmail_get(list_resp, []):
            result = gp.poll_agent_inbox(credentials_path="/fake/creds.json")
        assert result == gp._empty_inbox_summary()

    def test_includes_message_id_for_task_creation(self, tmp_path):
        list_resp = {"messages": [_stub("msg_abc")]}
        messages = [_message_response("msg_abc", subject="Invoice #7", from_="vendor@co.com")]

        with _patch_load_creds(), _patch_auth(), _patch_gmail_get(list_resp, messages):
            result = gp.poll_agent_inbox(credentials_path="/fake/creds.json")

        item = result["operational_items"][0]
        assert item["message_id"] == "msg_abc"

    def test_unsubscribable_email_treated_as_automated(self, tmp_path):
        list_resp = {"messages": [_stub("m3")]}
        messages = [
            _message_response("m3", subject="Weekly newsletter", from_="news@blog.com",
                               has_list_unsubscribe=True)
        ]
        with _patch_load_creds(), _patch_auth(), _patch_gmail_get(list_resp, messages):
            result = gp.poll_agent_inbox(credentials_path="/fake/creds.json")

        assert len(result["alerts"]) == 1


# ---------------------------------------------------------------------------
# poll_personal_inboxes — multi-account merge
# ---------------------------------------------------------------------------

class TestPollPersonalInboxes:
    def test_skips_missing_creds(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CREDENTIALS_PATH_PERSONAL_1", raising=False)
        monkeypatch.delenv("GOOGLE_CREDENTIALS_PATH_PERSONAL_2", raising=False)
        result = gp.poll_personal_inboxes()
        assert result == gp._empty_inbox_summary()

    def test_merges_two_inboxes(self, monkeypatch, tmp_path):
        p1 = _creds_file(tmp_path, "creds1.json")
        p2 = _creds_file(tmp_path, "creds2.json")
        monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH_PERSONAL_1", p1)
        monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH_PERSONAL_2", p2)

        list_resp = {"messages": [_stub("m1")]}
        messages = [_message_response("m1", subject="Hello from friend", from_="friend@example.com")]

        # Both accounts return one email
        call_count = 0

        def fake_gmail_get(url, token):
            nonlocal call_count
            responses = [list_resp, messages[0], list_resp, messages[0]]
            resp = responses[call_count] if call_count < len(responses) else {}
            call_count += 1
            return resp

        with _patch_load_creds(), _patch_auth(), \
             patch("src.agentic_os.gmail_poller._gmail_get", side_effect=fake_gmail_get):
            result = gp.poll_personal_inboxes()

        # Two accounts × one email each = 2 needs_reply items
        assert len(result["needs_reply"]) == 2

    def test_partial_creds_degrades_gracefully(self, monkeypatch, tmp_path):
        p1 = _creds_file(tmp_path, "creds1.json")
        monkeypatch.setenv("GOOGLE_CREDENTIALS_PATH_PERSONAL_1", p1)
        monkeypatch.delenv("GOOGLE_CREDENTIALS_PATH_PERSONAL_2", raising=False)

        list_resp = {"messages": [_stub("m1")]}
        messages = [_message_response("m1", subject="Call me later", from_="mum@example.com")]

        with _patch_load_creds(), _patch_auth(), _patch_gmail_get(list_resp, messages):
            result = gp.poll_personal_inboxes()

        # Only account 1 contributed
        assert len(result["needs_reply"]) == 1


# ---------------------------------------------------------------------------
# poll_all_inboxes
# ---------------------------------------------------------------------------

class TestPollAllInboxes:
    def test_returns_both_keys(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CREDENTIALS_PATH", raising=False)
        monkeypatch.delenv("GOOGLE_CREDENTIALS_PATH_PERSONAL_1", raising=False)
        monkeypatch.delenv("GOOGLE_CREDENTIALS_PATH_PERSONAL_2", raising=False)
        result = gp.poll_all_inboxes()
        assert "agent_inbox" in result
        assert "personal_inbox" in result
