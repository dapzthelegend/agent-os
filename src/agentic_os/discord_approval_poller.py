"""
Discord DM approval poller.

Polls the bot's DM channel with the operator for messages containing:
    APPROVE <approval_id>
    DENY <approval_id> [optional reason]

On each match, calls service.approve() or service.deny() and sends a
confirmation reply in the same DM channel.

State (last-seen Discord message snowflake) is persisted to
  <data_dir>/discord_dm_cursor.txt
so restarts don't re-process old messages.

Environment variables (loaded by config._load_env_file before the service
starts):
    DISCORD_BOT_TOKEN  — bot token for Discord REST API
    DISCORD_USER_ID    — operator's Discord user snowflake
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

from .approval_capability import mint_approval_token

log = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_BOT_TOKEN_ENV = "DISCORD_BOT_TOKEN"
DISCORD_USER_ID_ENV = "DISCORD_USER_ID"
# Discord's API is behind Cloudflare, which returns 403 for Python's default urllib UA.
DISCORD_USER_AGENT = "DiscordBot (https://github.com/agentic-os, 1.0)"

# Matches: APPROVE appr_abc123
_APPROVE_RE = re.compile(r"^\s*APPROVE\s+(\S+)\s*$", re.IGNORECASE)
# Matches: DENY appr_abc123   or   DENY appr_abc123 reason text here
_DENY_RE = re.compile(r"^\s*DENY\s+(\S+)(?:\s+(.+))?\s*$", re.IGNORECASE)

_CURSOR_FILENAME = "discord_dm_cursor.txt"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def poll_discord_approvals(paths, service) -> dict:
    """
    Fetch new DM messages from the operator and process any APPROVE/DENY commands.

    Returns a summary dict: {"processed": int, "errors": int, "skipped": int}
    """
    bot_token = os.environ.get(DISCORD_BOT_TOKEN_ENV)
    user_id = os.environ.get(DISCORD_USER_ID_ENV)

    if not bot_token or not user_id:
        log.debug("discord_approval_poller: env vars not set, skipping")
        return {"processed": 0, "errors": 0, "skipped": 0}

    cursor_path = Path(paths.data_dir) / _CURSOR_FILENAME

    # Step 1: open (or reuse) the DM channel
    channel_id, err = _get_dm_channel(bot_token=bot_token, user_id=user_id)
    if channel_id is None:
        log.warning("discord_approval_poller: could not open DM channel: %s", err)
        return {"processed": 0, "errors": 1, "skipped": 0}

    # Step 2: load the last-seen message snowflake
    after = _load_cursor(cursor_path)

    # Step 3: fetch messages newer than the cursor
    messages, err = _fetch_messages(channel_id, bot_token=bot_token, after=after)
    if messages is None:
        log.warning("discord_approval_poller: could not fetch messages: %s", err)
        return {"processed": 0, "errors": 1, "skipped": 0}

    if not messages:
        return {"processed": 0, "errors": 0, "skipped": 0}

    # Discord returns messages newest-first; process oldest-first so the cursor
    # advances correctly even if a later message fails.
    messages_asc = list(reversed(messages))

    summary = {"processed": 0, "errors": 0, "skipped": 0}
    last_seen_id: Optional[str] = after

    for msg in messages_asc:
        msg_id: str = msg["id"]
        # Skip bot's own messages
        if msg.get("author", {}).get("bot"):
            last_seen_id = msg_id
            continue

        content: str = msg.get("content", "").strip()
        outcome = _handle_message(content, service=service, channel_id=channel_id, bot_token=bot_token)

        if outcome == "processed":
            summary["processed"] += 1
        elif outcome == "error":
            summary["errors"] += 1
        else:
            summary["skipped"] += 1

        last_seen_id = msg_id

    # Step 4: persist cursor to the newest message we saw
    if last_seen_id and last_seen_id != after:
        _save_cursor(cursor_path, last_seen_id)

    log.info(
        "discord_approval_poller: processed=%d errors=%d skipped=%d",
        summary["processed"], summary["errors"], summary["skipped"],
    )
    return summary


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------

def _handle_message(
    content: str,
    *,
    service,
    channel_id: str,
    bot_token: str,
) -> str:
    """Parse and act on a single message. Returns 'processed', 'error', or 'skipped'."""
    approve_match = _APPROVE_RE.match(content)
    if approve_match:
        approval_id = approve_match.group(1)
        return _do_approve(approval_id, service=service, channel_id=channel_id, bot_token=bot_token)

    deny_match = _DENY_RE.match(content)
    if deny_match:
        approval_id = deny_match.group(1)
        reason = deny_match.group(2)  # may be None
        return _do_deny(approval_id, reason=reason, service=service, channel_id=channel_id, bot_token=bot_token)

    return "skipped"


def _do_approve(
    approval_id: str,
    *,
    service,
    channel_id: str,
    bot_token: str,
) -> str:
    try:
        service.approve(
            approval_id,
            approval_token=mint_approval_token(action="approve", approval_id=approval_id),
        )
        log.info("discord_approval_poller: approved %s", approval_id)
        _send_reply(f"✅ Approved `{approval_id}`.", channel_id=channel_id, bot_token=bot_token)
        return "processed"
    except Exception as exc:
        log.warning("discord_approval_poller: approve %s failed: %s", approval_id, exc)
        _send_reply(
            f"⚠️ Could not approve `{approval_id}`: {exc}",
            channel_id=channel_id,
            bot_token=bot_token,
        )
        return "error"


def _do_deny(
    approval_id: str,
    *,
    reason: Optional[str],
    service,
    channel_id: str,
    bot_token: str,
) -> str:
    try:
        service.deny(
            approval_id,
            decision_note=reason,
            approval_token=mint_approval_token(action="deny", approval_id=approval_id),
        )
        log.info("discord_approval_poller: denied %s (reason=%r)", approval_id, reason)
        note = f" — {reason}" if reason else ""
        _send_reply(f"❌ Denied `{approval_id}`{note}.", channel_id=channel_id, bot_token=bot_token)
        return "processed"
    except Exception as exc:
        log.warning("discord_approval_poller: deny %s failed: %s", approval_id, exc)
        _send_reply(
            f"⚠️ Could not deny `{approval_id}`: {exc}",
            channel_id=channel_id,
            bot_token=bot_token,
        )
        return "error"


# ---------------------------------------------------------------------------
# Discord REST helpers
# ---------------------------------------------------------------------------

def _get_dm_channel(*, bot_token: str, user_id: str) -> tuple[Optional[str], Optional[str]]:
    """Open (or fetch existing) DM channel with user_id. Returns (channel_id, error)."""
    try:
        payload = json.dumps({"recipient_id": user_id}).encode()
        req = urllib_request.Request(
            f"{DISCORD_API_BASE}/users/@me/channels",
            data=payload,
            headers={
                "Authorization": f"Bot {bot_token}",
                "Content-Type": "application/json",
                "User-Agent": DISCORD_USER_AGENT,
            },
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        channel_id = data.get("id")
        if not channel_id:
            return None, f"no id in response: {data}"
        return channel_id, None
    except urllib_error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        return None, f"HTTP {exc.code}: {body}"
    except urllib_error.URLError as exc:
        return None, str(exc)


def _fetch_messages(
    channel_id: str,
    *,
    bot_token: str,
    after: Optional[str],
    limit: int = 50,
) -> tuple[Optional[list], Optional[str]]:
    """
    Fetch up to `limit` messages from the channel newer than `after`.
    Returns (messages_list, error). Messages are ordered newest-first.
    """
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages?limit={limit}"
    if after:
        url += f"&after={after}"
    try:
        req = urllib_request.Request(
            url,
            headers={
                "Authorization": f"Bot {bot_token}",
                "User-Agent": DISCORD_USER_AGENT,
            },
            method="GET",
        )
        with urllib_request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode()), None
    except urllib_error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        return None, f"HTTP {exc.code}: {body}"
    except urllib_error.URLError as exc:
        return None, str(exc)


def _send_reply(text: str, *, channel_id: str, bot_token: str) -> None:
    """Post a reply to the DM channel. Logs on failure; never raises."""
    try:
        payload = json.dumps({"content": text}).encode()
        req = urllib_request.Request(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            data=payload,
            headers={
                "Authorization": f"Bot {bot_token}",
                "Content-Type": "application/json",
                "User-Agent": DISCORD_USER_AGENT,
            },
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=10):
            pass
    except Exception as exc:
        log.warning("discord_approval_poller: reply send failed: %s", exc)


# ---------------------------------------------------------------------------
# Cursor persistence
# ---------------------------------------------------------------------------

def _load_cursor(cursor_path: Path) -> Optional[str]:
    """Return the last-seen message snowflake, or None if not set."""
    try:
        return cursor_path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def _save_cursor(cursor_path: Path, message_id: str) -> None:
    cursor_path.write_text(message_id, encoding="utf-8")
