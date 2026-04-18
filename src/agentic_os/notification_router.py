"""
Smart notification router — priority chain: Discord DM → stderr.

Routing rules:
  approval_requested  → Discord DM only (no email)
  task_completed      → Discord DM only (no email)
  task_failed         → Discord DM only (no email)
  overdue_task        → Discord DM only (no email)
  approval_reminder   → Discord DM only (no email)

Environment variables (read at call time, not import time):
  DISCORD_BOT_TOKEN   — Bot token for Discord REST API
  DISCORD_USER_ID     — Target user's snowflake ID for DM delivery
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
# Discord's API is behind Cloudflare, which blocks Python's default urllib UA with 403.
# All requests must identify as a Discord bot per the API docs.
DISCORD_USER_AGENT = "DiscordBot (https://github.com/agentic-os, 1.0)"
DISCORD_OVERDUE_PUSH_ENABLED_ENV = "DISCORD_OVERDUE_PUSH_ENABLED"
DISCORD_APPROVAL_REMINDER_PUSH_ENABLED_ENV = "DISCORD_APPROVAL_REMINDER_PUSH_ENABLED"

APPROVAL_REMINDER_THRESHOLD_HOURS = 1.0
OVERDUE_THRESHOLD_HOURS = 48.0


# ---------------------------------------------------------------------------
# Delivery result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NotificationResult:
    channel: str          # discord | stderr
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
    Send approval request notification via Discord DM with interactive buttons.

    If DISCORD_APPLICATION_ID is set (i.e. the Interactions endpoint is live)
    the DM is sent as a rich embed with Approve/Deny/Open buttons. Otherwise
    falls back to plain text with APPROVE/DENY instructions for the poller.
    """
    title = _task_title(task)

    # Prefer the buttoned payload when the Interactions endpoint is configured.
    if os.environ.get("DISCORD_APPLICATION_ID"):
        from .discord_interactions import approval_request_payload

        payload = approval_request_payload(task=task, approval=approval)
        fallback_text = (
            f"⏳ Approval required: {title}\n"
            f"Task `{task.id}` · Approval `{approval.id}`"
        )
        return _send_discord_payload(payload, fallback_text=fallback_text)

    # Legacy text fallback (APPROVE/DENY poller path)
    lines = [
        f"⏳ **Approval required**: {title}",
        f"Task ID: `{task.id}`",
        f"Approval ID: `{approval.id}`",
        f"Domain: {task.domain} | Risk: {task.risk_level}",
        "",
        f"To approve: reply **APPROVE {approval.id}** in this chat",
        f"To deny: reply **DENY {approval.id}** or **DENY {approval.id} <reason>** in this chat",
    ]
    message = "\n".join(lines)
    return _send_discord_only(message)


def route_task_completed(
    task: TaskRecord,
    *,
    result_link: Optional[str] = None,
    word_count: Optional[int] = None,
) -> NotificationResult:
    """Send task completion notification via Discord DM only (no email)."""
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
    return _send_discord_only(message)


def route_task_failed(task: TaskRecord) -> NotificationResult:
    """Send task failure alert via Discord DM only (no email)."""
    title = _task_title(task)
    reason = (task.result_summary or "no reason recorded")[:200]
    message = (
        f"❌ **Task failed**: {title}\n"
        f"Task ID: `{task.id}`\n"
        f"Reason: {reason}"
    )
    return _send_discord_only(message)


def route_overdue_task(
    task: TaskRecord,
    hours_overdue: float,
) -> NotificationResult:
    """Send overdue task alert via Discord DM only (no email)."""
    if not _is_env_truthy(DISCORD_OVERDUE_PUSH_ENABLED_ENV):
        return NotificationResult(
            channel="disabled",
            success=False,
            message="overdue push notifications disabled",
        )

    title = _task_title(task)
    message = (
        f"🕐 **Overdue task** ({hours_overdue:.0f}h no update): {title}\n"
        f"Task ID: `{task.id}`\n"
        f"Status: {task.status} | Domain: {task.domain}\n"
        "Action needed: check, complete, or cancel this task."
    )
    return _send_discord_only(message)


def route_approval_reminder(
    task: TaskRecord,
    approval: ApprovalRecord,
    hours_pending: float,
) -> NotificationResult:
    """
    Send approval reminder via Discord DM only (no email).
    Only sends if actually overdue; caller is responsible for checking threshold.
    """
    if not _is_env_truthy(DISCORD_APPROVAL_REMINDER_PUSH_ENABLED_ENV):
        return NotificationResult(
            channel="disabled",
            success=False,
            message="approval reminder push notifications disabled",
        )

    title = _task_title(task)
    message = (
        f"🔔 **Approval reminder** ({hours_pending:.0f}h pending): {title}\n"
        f"Approval ID: `{approval.id}`\n"
        "This approval is still waiting for your decision."
    )
    return _send_discord_only(message)


# ---------------------------------------------------------------------------
# Internal delivery chain
# ---------------------------------------------------------------------------

def _send_discord_only(message: str) -> NotificationResult:
    """Try Discord DM; log to stderr on failure. No email fallback."""
    bot_token = os.environ.get(DISCORD_BOT_TOKEN_ENV)
    user_id = os.environ.get(DISCORD_USER_ID_ENV)
    if bot_token and user_id:
        ok, err = _send_discord_dm(message, bot_token=bot_token, user_id=user_id)
        if ok:
            return NotificationResult(channel="discord", success=True, message=message)
        print(f"[notification_router] Discord DM failed: {err}", file=sys.stderr)
    print(f"[notification_router] {message}", file=sys.stderr)
    return NotificationResult(channel="stderr", success=False, message=message, error="discord_failed")


def _is_env_truthy(name: str, *, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _send_with_fallback(message: str, *, subject: str = "") -> NotificationResult:
    """Compatibility shim; subject is ignored and delivery stays Discord-only."""
    del subject
    return _send_discord_only(message)


def _send_discord_payload(
    payload: dict,
    *,
    fallback_text: str,
) -> NotificationResult:
    """
    Send a rich Discord DM with embeds + components.

    payload must be a dict with keys like `embeds` and `components`. On
    delivery failure, falls back to plain text via `_send_discord_only`.
    """
    bot_token = os.environ.get(DISCORD_BOT_TOKEN_ENV)
    user_id = os.environ.get(DISCORD_USER_ID_ENV)
    if not (bot_token and user_id):
        return _send_discord_only(fallback_text)

    channel_id, err = _open_dm_channel(bot_token=bot_token, user_id=user_id)
    if channel_id is None:
        print(f"[notification_router] DM open failed: {err}", file=sys.stderr)
        return _send_discord_only(fallback_text)

    body = {"content": fallback_text, **payload}
    ok, err = _post_channel_message(
        channel_id, body=body, bot_token=bot_token
    )
    if ok:
        return NotificationResult(channel="discord", success=True, message=fallback_text)
    print(f"[notification_router] rich DM failed: {err}", file=sys.stderr)
    return _send_discord_only(fallback_text)


def _open_dm_channel(
    *, bot_token: str, user_id: str
) -> tuple[Optional[str], Optional[str]]:
    try:
        req = urllib_request.Request(
            f"{DISCORD_API_BASE}/users/@me/channels",
            data=json.dumps({"recipient_id": user_id}).encode(),
            headers={
                "Authorization": f"Bot {bot_token}",
                "Content-Type": "application/json",
                "User-Agent": DISCORD_USER_AGENT,
            },
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        cid = data.get("id")
        if not cid:
            return None, f"no id in response: {data}"
        return cid, None
    except urllib_error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        return None, f"HTTP {exc.code}: {body}"
    except urllib_error.URLError as exc:
        return None, str(exc)


def _post_channel_message(
    channel_id: str,
    *,
    body: dict,
    bot_token: str,
) -> tuple[bool, Optional[str]]:
    try:
        req = urllib_request.Request(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bot {bot_token}",
                "Content-Type": "application/json",
                "User-Agent": DISCORD_USER_AGENT,
            },
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 201), None
    except urllib_error.HTTPError as exc:
        err_body = exc.read().decode(errors="replace")
        return False, f"HTTP {exc.code}: {err_body}"
    except urllib_error.URLError as exc:
        return False, str(exc)


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
                "User-Agent": DISCORD_USER_AGENT,
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
                "User-Agent": DISCORD_USER_AGENT,
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
