"""
Gmail inbox poller using Gmail REST API + OAuth2 refresh-token flow.

Fetches unread emails from the last 24 hours for up to three accounts
and returns normalized InboxSummary-compatible dicts for DailyRoutineInput.

Credential resolution (one JSON file per account, keyed by env var):
  GOOGLE_CREDENTIALS_PATH          → franchieinc@gmail.com (agent inbox, full access)
  GOOGLE_CREDENTIALS_PATH_PERSONAL_1 → dapzthelegend@gmail.com (read only)
  GOOGLE_CREDENTIALS_PATH_PERSONAL_2 → solaaremuoluwadara@gmail.com (read only)

Each credentials file must contain a top-level "web" dict with:
  client_id, client_secret, refresh_token

If a personal inbox credentials env var is unset, that inbox is skipped
gracefully and returns an empty InboxSummary.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

from .gmail_sender import _load_credentials, _refresh_access_token

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# Per-account env vars
_AGENT_CREDS_ENV = "GOOGLE_CREDENTIALS_PATH"
_PERSONAL_1_CREDS_ENV = "GOOGLE_CREDENTIALS_PATH_PERSONAL_1"
_PERSONAL_2_CREDS_ENV = "GOOGLE_CREDENTIALS_PATH_PERSONAL_2"

_PERSONAL_ACCOUNT_ENVS = [_PERSONAL_1_CREDS_ENV, _PERSONAL_2_CREDS_ENV]

# Patterns that identify automated / no-reply senders
_AUTOMATED_PATTERNS = (
    "noreply@",
    "no-reply@",
    "do-not-reply@",
    "donotreply@",
    "notifications@",
    "notification@",
    "alerts@",
    "alert@",
    "mailer-daemon",
    "automated@",
    "newsletter@",
    "updates@",
    "reply@",  # transactional-style catch-all
    "bounce@",
    "postmaster@",
)

# Subject keywords that signal urgency
_URGENT_KEYWORDS = (
    "urgent",
    "asap",
    "immediately",
    "critical",
    "action required",
    "action needed",
    "deadline",
    "overdue",
    "time-sensitive",
    "expires today",
    "response required",
)

# Subject keywords that signal operational work for the agent inbox
_OPERATIONAL_KEYWORDS = (
    "invoice",
    "contract",
    "agreement",
    "payment",
    "approve",
    "approval",
    "confirm",
    "signature",
    "sign",
    "proposal",
    "quote",
    "request",
    "follow up",
    "follow-up",
    "action",
    "reminder",
)

# Subject keywords that signal alert/automated content for the agent inbox
_ALERT_KEYWORDS = (
    "alert",
    "warning",
    "error",
    "failed",
    "report",
    "summary",
    "digest",
    "notification",
    "receipt",
    "order",
    "shipped",
    "delivered",
    "statement",
    "bill",
    "renewal",
)

_MAX_MESSAGES = 50


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def poll_agent_inbox(credentials_path: Optional[str] = None) -> dict:
    """
    Poll franchieinc@gmail.com (agent inbox) for unread messages from the last 24h.

    Returns a normalized InboxSummary-compatible dict:
      {operational_items: [...], alerts: [...], urgent: [], needs_reply: [], important_fyi: []}
    """
    path = credentials_path or os.environ.get(_AGENT_CREDS_ENV)
    if not path:
        _warn("GOOGLE_CREDENTIALS_PATH not set; agent inbox will be empty")
        return _empty_inbox_summary()
    return _fetch_and_classify(path, mailbox_kind="agent")


def poll_personal_inboxes() -> dict:
    """
    Poll personal inboxes (dapzthelegend + solaaremuoluwadara) and merge results.

    Returns a merged normalized InboxSummary-compatible dict:
      {urgent: [...], needs_reply: [...], important_fyi: [...], operational_items: [], alerts: []}

    Inboxes whose credentials env var is unset are silently skipped.
    """
    merged: dict[str, list] = {
        "urgent": [],
        "needs_reply": [],
        "important_fyi": [],
        "operational_items": [],
        "alerts": [],
    }
    for env_var in _PERSONAL_ACCOUNT_ENVS:
        path = os.environ.get(env_var)
        if not path:
            _warn(f"{env_var} not set; skipping that personal inbox")
            continue
        result = _fetch_and_classify(path, mailbox_kind="personal")
        for key in merged:
            merged[key].extend(result.get(key, []))
    return merged


def poll_all_inboxes() -> dict:
    """
    Poll all three Gmail accounts and return structured inbox data.

    Returns:
      {
        "agent_inbox":    <InboxSummary dict for franchieinc@gmail.com>,
        "personal_inbox": <merged InboxSummary dict for personal accounts>,
      }
    """
    return {
        "agent_inbox": poll_agent_inbox(),
        "personal_inbox": poll_personal_inboxes(),
    }


# ---------------------------------------------------------------------------
# Internal fetch + classify pipeline
# ---------------------------------------------------------------------------

def _fetch_and_classify(credentials_path: str, *, mailbox_kind: str) -> dict:
    try:
        creds = _load_credentials(credentials_path)
        token = _refresh_access_token(
            client_id=creds["client_id"],
            client_secret=creds["client_secret"],
            refresh_token=creds["refresh_token"],
        )
    except Exception as exc:  # noqa: BLE001
        _warn(f"Could not authenticate for {credentials_path!r}: {exc}")
        return _empty_inbox_summary()

    try:
        message_stubs = _list_unread_messages(token)
    except Exception as exc:  # noqa: BLE001
        _warn(f"Failed to list messages: {exc}")
        return _empty_inbox_summary()

    emails: list[dict] = []
    for stub in message_stubs[:_MAX_MESSAGES]:
        try:
            msg = _fetch_message_metadata(token, stub["id"])
            if msg:
                emails.append(msg)
        except Exception as exc:  # noqa: BLE001
            _warn(f"Skipping message {stub['id']}: {exc}")

    if mailbox_kind == "agent":
        return _classify_agent_inbox(emails)
    return _classify_personal_inbox(emails)


def _list_unread_messages(token: str) -> list[dict]:
    """List unread inbox messages from the last 24h."""
    url = (
        f"{GMAIL_API_BASE}/messages"
        "?q=is%3Aunread+newer_than%3A1d"
        "&labelIds=INBOX"
        f"&maxResults={_MAX_MESSAGES}"
    )
    data = _gmail_get(url, token)
    return data.get("messages", [])


def _fetch_message_metadata(token: str, message_id: str) -> Optional[dict]:
    """Fetch subject, from, date, snippet for a single message."""
    url = (
        f"{GMAIL_API_BASE}/messages/{message_id}"
        "?format=metadata"
        "&metadataHeaders=Subject"
        "&metadataHeaders=From"
        "&metadataHeaders=Date"
        "&metadataHeaders=List-Unsubscribe"
    )
    data = _gmail_get(url, token)
    headers = {
        h["name"].lower(): h["value"]
        for h in data.get("payload", {}).get("headers", [])
    }
    subject = headers.get("subject", "").strip()
    from_raw = headers.get("from", "").strip()
    date_raw = headers.get("date", "").strip()
    snippet = data.get("snippet", "").strip()
    is_unsubscribable = bool(headers.get("list-unsubscribe"))

    sender_name, sender_email = _parse_from(from_raw)
    return {
        "id": message_id,
        "subject": subject or "(no subject)",
        "sender": from_raw,
        "sender_email": sender_email,
        "sender_name": sender_name,
        "date": date_raw,
        "snippet": snippet,
        "is_automated": _is_automated_sender(sender_email) or is_unsubscribable,
        "is_urgent": _has_urgent_keywords(subject),
    }


# ---------------------------------------------------------------------------
# Classification — agent inbox (franchieinc@gmail.com)
# ---------------------------------------------------------------------------

def _classify_agent_inbox(emails: list[dict]) -> dict:
    result: dict[str, list] = _empty_inbox_summary()
    for email in emails:
        item = _to_inbox_item(email)
        subject_lower = email["subject"].lower()
        if email["is_urgent"]:
            item["actionable"] = True
            result["operational_items"].append(item)
        elif email["is_automated"]:
            result["alerts"].append(item)
        elif any(kw in subject_lower for kw in _OPERATIONAL_KEYWORDS):
            item["actionable"] = True
            result["operational_items"].append(item)
        elif any(kw in subject_lower for kw in _ALERT_KEYWORDS):
            result["alerts"].append(item)
        else:
            # Real person email in agent inbox — treat as operational
            item["actionable"] = not email["is_automated"]
            result["operational_items"].append(item)
    return result


# ---------------------------------------------------------------------------
# Classification — personal inboxes
# ---------------------------------------------------------------------------

def _classify_personal_inbox(emails: list[dict]) -> dict:
    result: dict[str, list] = _empty_inbox_summary()
    for email in emails:
        item = _to_inbox_item(email)
        if email["is_urgent"]:
            item["actionable"] = True
            result["urgent"].append(item)
        elif email["is_automated"]:
            result["important_fyi"].append(item)
        else:
            # Real person → needs reply
            item["actionable"] = True
            result["needs_reply"].append(item)
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_inbox_item(email: dict) -> dict:
    return {
        "message_id": email["id"],
        "subject": email["subject"],
        "sender": email["sender"],
        "summary": email["snippet"][:200] if email["snippet"] else None,
        "requested_action": None,
        "due": None,
        "actionable": False,
    }


def _empty_inbox_summary() -> dict:
    return {
        "urgent": [],
        "needs_reply": [],
        "important_fyi": [],
        "operational_items": [],
        "alerts": [],
    }


def _parse_from(from_raw: str) -> tuple[str, str]:
    """Extract (name, email) from a From header like 'John <john@example.com>'."""
    m = re.search(r"<([^>]+)>", from_raw)
    if m:
        email = m.group(1).strip().lower()
        name = from_raw[: m.start()].strip().strip('"').strip()
        return name, email
    email = from_raw.strip().lower()
    return email, email


def _is_automated_sender(sender_email: str) -> bool:
    lowered = sender_email.lower()
    return any(pat in lowered for pat in _AUTOMATED_PATTERNS)


def _has_urgent_keywords(subject: str) -> bool:
    lowered = subject.lower()
    return any(kw in lowered for kw in _URGENT_KEYWORDS)


def _gmail_get(url: str, token: str) -> dict:
    req = urllib_request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib_request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib_error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"Gmail API HTTP {exc.code}: {body}") from exc


def _warn(msg: str) -> None:
    print(f"[gmail_poller] {msg}", file=sys.stderr)
