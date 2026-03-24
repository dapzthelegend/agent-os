"""
Smart notification router — priority chain: Discord DM → Gmail → stderr.

Routing rules:
  approval_requested  → Discord DM (approve/deny instructions) + Gmail fallback
  task_completed      → Discord DM → Gmail fallback
  task_failed         → Discord DM → Gmail fallback
  overdue_task        → Discord DM → Gmail fallback
  approval_reminder   → Discord DM → Gmail fallback

Environment variables (read at call time, not import time):
  DISCORD_BOT_TOKEN   — Bot token for Discord REST API
  DISCORD_USER_ID     — Target user's snowflake ID for DM delivery
  GOOGLE_CREDENTIALS_PATH — Path to gog.json (OAuth2 web client credentials)
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

from .models import ApprovalRecord, TaskRecord

DISCORD_BOT_TOKEN_ENV = "DISCORD_BOT_TOKEN"
DISCORD_USER_ID_ENV = "DISCORD_USER_ID"
DISCORD_API_BASE = "https://discord.com/api/v10"

APPROVAL_REMINDER_THRESHOLD_HOURS = 1.0
OVERDUE_THRESHOLD_HOURS = 48.0


# ---------------------------------------------------------------------------
# Delivery result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NotificationResult:
    channel: str          # discord | gmail | stderr
    success: bool
    message: str
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Public routing interface
# ---------------------------------------------------------------------------

def route_approval_requested(
    task: TaskRecord,
    approval: ApprovalRecord,
) -> NotificationResult:
    """
    Send approval request notification.

    Priority: Discord DM → Gmail fallback.
    Message includes task summary, approval_id, and plain-text approve/deny
    instructions for email reply or Discord reaction.
    """
    title = _task_title(task)
    lines = [
        f"⏳ **Approval required**: {title}",
        f"Task ID: `{task.id}`",
        f"Approval ID: `{approval.id}`",
        f"Domain: {task.domain} | Risk: {task.risk_level}",
        "",
        "To approve: reply **APPROVE** to the notification email, or run:",
        f"  `python3 -m agentic_os.cli approval approve {approval.id}`",
        "To deny: reply **DENY**, or run:",
        f"  `python3 -m agentic_os.cli approval deny {approval.id}`",
    ]
    message = "\n".join(lines)
    subject = f"[Agent] Approval required: {title}"
    return _send_with_fallback(message, subject=subject)


def route_task_completed(
    task: TaskRecord,
    *,
    result_link: Optional[str] = None,
    word_count: Optional[int] = None,
) -> NotificationResult:
    """Send task completion notification via Discord DM → Gmail."""
    title = _task_title(task)
    lines = [f"✅ **Task completed**: {title}"]
    if task.domain == "content" or task.intent_type == "content":
        if word_count:
            lines.append(f"Content produced: {word_count:,} words")
    if task.result_summary:
        summary = task.result_summary[:200].replace("\n", " ")
        lines.append(f"Result: {summary}")
    if result_link:
        lines.append(f"→ {result_link}")
    message = "\n".join(lines)
    subject = f"[Agent] Task completed: {title}"
    return _send_with_fallback(message, subject=subject)


def route_task_failed(task: TaskRecord) -> NotificationResult:
    """Send task failure alert via Discord DM → Gmail."""
    title = _task_title(task)
    reason = (task.result_summary or "no reason recorded")[:200]
    message = (
        f"❌ **Task failed**: {title}\n"
        f"Task ID: `{task.id}`\n"
        f"Reason: {reason}"
    )
    subject = f"[Agent] Task failed: {title}"
    return _send_with_fallback(message, subject=subject)


def route_overdue_task(
    task: TaskRecord,
    hours_overdue: float,
) -> NotificationResult:
    """Send overdue task alert via Discord DM → Gmail (fires when >48h no update)."""
    title = _task_title(task)
    message = (
        f"🕐 **Overdue task** ({hours_overdue:.0f}h no update): {title}\n"
        f"Task ID: `{task.id}`\n"
        f"Status: {task.status} | Domain: {task.domain}\n"
        "Action needed: check, complete, or cancel this task."
    )
    subject = f"[Agent] Overdue task ({hours_overdue:.0f}h): {title}"
    return _send_with_fallback(message, subject=subject)


def route_approval_reminder(
    task: TaskRecord,
    approval: ApprovalRecord,
    hours_pending: float,
) -> NotificationResult:
    """
    Send approval reminder if no decision after APPROVAL_REMINDER_THRESHOLD_HOURS.
    Only sends if actually overdue; caller is responsible for checking threshold.
    """
    title = _task_title(task)
    message = (
        f"🔔 **Approval reminder** ({hours_pending:.0f}h pending): {title}\n"
        f"Approval ID: `{approval.id}`\n"
        "This approval is still waiting for your decision."
    )
    subject = f"[Agent] Approval reminder ({hours_pending:.0f}h): {title}"
    return _send_with_fallback(message, subject=subject)


# ---------------------------------------------------------------------------
# Internal delivery chain
# ---------------------------------------------------------------------------

def _send_with_fallback(message: str, *, subject: str = "") -> NotificationResult:
    """Try Discord DM; fall back to Gmail; last resort: stderr."""
    # --- Discord Bot DM ---
    bot_token = os.environ.get(DISCORD_BOT_TOKEN_ENV)
    user_id = os.environ.get(DISCORD_USER_ID_ENV)
    if bot_token and user_id:
        ok, err = _send_discord_dm(message, bot_token=bot_token, user_id=user_id)
        if ok:
            return NotificationResult(channel="discord", success=True, message=message)
        # log discord failure but continue to Gmail
        print(f"[notification_router] Discord DM failed: {err}", file=sys.stderr)

    # --- Gmail fallback ---
    try:
        from .gmail_sender import send_email  # lazy import keeps startup fast
        email_subject = subject or "[Agent] Notification"
        ok = send_email(
            to="franchieinc@gmail.com",
            subject=email_subject,
            body=_strip_discord_markdown(message),
        )
        if ok:
            return NotificationResult(channel="gmail", success=True, message=message)
    except Exception as exc:  # noqa: BLE001
        print(f"[notification_router] Gmail fallback failed: {exc}", file=sys.stderr)

    # --- Last resort: stderr ---
    print(f"[notification_router] {subject}: {message}", file=sys.stderr)
    return NotificationResult(
        channel="stderr",
        success=False,
        message=message,
        error="all_channels_failed",
    )


def _send_discord_dm(
    message: str,
    *,
    bot_token: str,
    user_id: str,
) -> tuple[bool, Optional[str]]:
    """
    Open a DM channel with user_id then post message.

    Returns (success, error_str).
    """
    # Step 1: create/fetch DM channel
    try:
        dm_payload = json.dumps({"recipient_id": user_id}).encode()
        dm_req = urllib_request.Request(
            f"{DISCORD_API_BASE}/users/@me/channels",
            data=dm_payload,
            headers={
                "Authorization": f"Bot {bot_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib_request.urlopen(dm_req, timeout=10) as resp:
            channel_data = json.loads(resp.read().decode())
        channel_id = channel_data.get("id")
        if not channel_id:
            return False, f"No channel id in response: {channel_data}"
    except (urllib_error.URLError, urllib_error.HTTPError) as exc:
        return False, str(exc)

    # Step 2: send message to DM channel
    try:
        msg_payload = json.dumps({"content": message}).encode()
        msg_req = urllib_request.Request(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            data=msg_payload,
            headers={
                "Authorization": f"Bot {bot_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib_request.urlopen(msg_req, timeout=10) as resp:
            return resp.status in (200, 201), None
    except urllib_error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        return False, f"HTTP {exc.code}: {body}"
    except urllib_error.URLError as exc:
        return False, str(exc)


def _strip_discord_markdown(text: str) -> str:
    """Remove Discord markdown formatting for plain-text email body."""
    return text.replace("**", "").replace("`", "").replace("__", "")


def _task_title(task: TaskRecord) -> str:
    return (task.user_request.splitlines()[0].strip() or task.id)[:100]


# ---------------------------------------------------------------------------
# Bulk helpers for service integration
# ---------------------------------------------------------------------------

def notify_overdue_tasks(
    tasks: list[TaskRecord],
    *,
    overdue_threshold_hours: float = OVERDUE_THRESHOLD_HOURS,
) -> list[NotificationResult]:
    """Send overdue alerts for a list of tasks."""
    results = []
    for task in tasks:
        results.append(route_overdue_task(task, hours_overdue=overdue_threshold_hours))
    return results
