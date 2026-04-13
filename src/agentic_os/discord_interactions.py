"""
Discord Interactions endpoint — interactive panel / buttons / modals.

This module replaces (augments) the polling-based approve/deny flow with a
webhook-driven Interactions model:

    Discord  ──(Ed25519-signed POST)──▶  /discord/interactions (FastAPI)
                                                │
                                                ├─ PING  ─────────────────────▶ PONG
                                                ├─ SLASH /panel ─────────────▶ render main panel
                                                ├─ BUTTON approve:<id> ──────▶ service.approve()
                                                ├─ BUTTON deny:<id> ─────────▶ deny-reason modal
                                                ├─ MODAL deny_confirm:<id> ──▶ service.deny(reason)
                                                └─ BUTTON panel:<view> ──────▶ re-render panel

Requires env vars:
    DISCORD_BOT_TOKEN        — bot token (for followup webhooks + admin calls)
    DISCORD_USER_ID          — operator's user id (restrict who can press buttons)
    DISCORD_PUBLIC_KEY       — application public key (Ed25519 verification)
    DISCORD_APPLICATION_ID   — application id (for followup webhook URLs + registration)
    AGENTIC_OS_PUBLIC_URL    — public base URL for deeplinks in embeds
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from typing import Any, Iterable, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Discord constants
# ---------------------------------------------------------------------------

# Interaction types
TYPE_PING = 1
TYPE_APPLICATION_COMMAND = 2
TYPE_MESSAGE_COMPONENT = 3
TYPE_MODAL_SUBMIT = 5

# Interaction response types
RESP_PONG = 1
RESP_CHANNEL_MESSAGE = 4
RESP_DEFERRED_UPDATE = 6
RESP_UPDATE_MESSAGE = 7
RESP_MODAL = 9

# Component types
COMPONENT_ACTION_ROW = 1
COMPONENT_BUTTON = 2
COMPONENT_TEXT_INPUT = 4

# Button styles
BTN_PRIMARY = 1
BTN_SECONDARY = 2
BTN_SUCCESS = 3
BTN_DANGER = 4
BTN_LINK = 5

# Message flags
FLAG_EPHEMERAL = 1 << 6  # 64

# Embed colors (decimal RGB)
COLOR_PENDING = 0xF1C40F  # amber
COLOR_SUCCESS = 0x2ECC71  # green
COLOR_DANGER = 0xE74C3C  # red
COLOR_INFO = 0x3498DB  # blue
COLOR_MUTED = 0x95A5A6  # grey


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

class SignatureError(Exception):
    """Raised when Ed25519 signature verification fails."""


def verify_signature(
    *,
    public_key_hex: str,
    signature_hex: str,
    timestamp: str,
    body: bytes,
) -> None:
    """
    Verify a Discord interaction signature.

    Discord signs `timestamp + body` with Ed25519; the application's public
    key is published in the Developer Portal under "General Information".
    Raises SignatureError on any failure.
    """
    try:
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        signature = bytes.fromhex(signature_hex)
        message = timestamp.encode("utf-8") + body
        pk.verify(signature, message)
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise SignatureError(f"invalid signature: {exc}") from exc


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

def _interaction_user_id(interaction: dict) -> Optional[str]:
    """
    Extract the invoking user id from an interaction payload.

    Interaction can come from a DM (`user` at top level) or from a guild
    (`member.user`). Returns the string snowflake or None.
    """
    user = interaction.get("user")
    if user and isinstance(user, dict):
        return user.get("id")
    member = interaction.get("member")
    if member and isinstance(member, dict):
        inner = member.get("user")
        if isinstance(inner, dict):
            return inner.get("id")
    return None


def _require_operator(interaction: dict) -> Optional[dict]:
    """
    Check that the interaction was invoked by the configured operator.

    Returns None if allowed, or an ephemeral refusal response dict otherwise.
    """
    operator_id = os.environ.get("DISCORD_USER_ID")
    caller_id = _interaction_user_id(interaction)
    if operator_id and caller_id and caller_id == operator_id:
        return None
    log.warning(
        "discord_interactions: unauthorized caller %r (expected %r)",
        caller_id, operator_id,
    )
    return _ephemeral("⛔ You are not authorized to control agentic-os.")


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------

def handle_interaction(interaction: dict, *, service) -> dict:
    """
    Dispatch a verified interaction payload to the appropriate handler.

    Returns a dict ready to be JSON-serialized as the HTTP response body.
    """
    itype = interaction.get("type")

    if itype == TYPE_PING:
        return {"type": RESP_PONG}

    if itype == TYPE_APPLICATION_COMMAND:
        refusal = _require_operator(interaction)
        if refusal is not None:
            return refusal
        return _handle_slash_command(interaction, service=service)

    if itype == TYPE_MESSAGE_COMPONENT:
        refusal = _require_operator(interaction)
        if refusal is not None:
            return refusal
        return _handle_component(interaction, service=service)

    if itype == TYPE_MODAL_SUBMIT:
        refusal = _require_operator(interaction)
        if refusal is not None:
            return refusal
        return _handle_modal(interaction, service=service)

    log.warning("discord_interactions: unknown interaction type %r", itype)
    return _ephemeral(f"Unsupported interaction type `{itype}`.")


# ---------------------------------------------------------------------------
# Slash command handler
# ---------------------------------------------------------------------------

def _handle_slash_command(interaction: dict, *, service) -> dict:
    data = interaction.get("data") or {}
    name = (data.get("name") or "").lower()

    if name == "panel":
        return _panel_main(service, ephemeral=False)
    if name == "pending":
        return _panel_pending(service)
    if name == "tasks":
        return _panel_in_progress(service)
    if name == "status":
        return _panel_status(service)
    if name == "failures":
        return _panel_failures(service)
    if name == "overdue":
        return _panel_overdue(service)

    return _ephemeral(f"Unknown command `/{name}`.")


# ---------------------------------------------------------------------------
# Message component handler (buttons)
# ---------------------------------------------------------------------------

def _handle_component(interaction: dict, *, service) -> dict:
    data = interaction.get("data") or {}
    custom_id: str = data.get("custom_id") or ""
    action, _, arg = custom_id.partition(":")

    # Approve button → run approval, update message in place
    if action == "approve" and arg:
        try:
            service.approve(arg)
            return _render_resolved(
                title=f"✅ Approved `{arg}`",
                color=COLOR_SUCCESS,
                update=True,
            )
        except Exception as exc:
            log.warning("discord_interactions: approve %s failed: %s", arg, exc)
            return _ephemeral(f"⚠️ Could not approve `{arg}`: {exc}")

    # Deny button → open a modal to capture reason
    if action == "deny" and arg:
        return _deny_modal(arg)

    # Cancel button → cancel the approval altogether
    if action == "cancel_approval" and arg:
        try:
            service.cancel(arg, decision_note="cancelled from Discord")
            return _render_resolved(
                title=f"🚫 Cancelled `{arg}`",
                color=COLOR_MUTED,
                update=True,
            )
        except Exception as exc:
            return _ephemeral(f"⚠️ Could not cancel `{arg}`: {exc}")

    # Panel navigation
    if action == "panel":
        view = arg or "home"
        if view == "home":
            return _panel_main(service, update=True)
        if view == "pending":
            return _panel_pending(service, update=True)
        if view == "in_progress":
            return _panel_in_progress(service, update=True)
        if view == "failures":
            return _panel_failures(service, update=True)
        if view == "overdue":
            return _panel_overdue(service, update=True)
        if view == "status":
            return _panel_status(service, update=True)
        return _ephemeral(f"Unknown panel view `{view}`.")

    # Task actions from panel rows
    if action == "task_cancel" and arg:
        try:
            service.operator_close_task(arg, reason="Cancelled from Discord panel")
            return _ephemeral(f"🚫 Cancelled task `{arg}`.")
        except Exception as exc:
            return _ephemeral(f"⚠️ Could not cancel task `{arg}`: {exc}")

    if action == "task_retry" and arg:
        try:
            service.retry_task(arg, feedback="retry from Discord panel")
            return _ephemeral(f"🔁 Retrying task `{arg}`.")
        except Exception as exc:
            return _ephemeral(f"⚠️ Could not retry task `{arg}`: {exc}")

    return _ephemeral(f"Unknown action `{custom_id}`.")


# ---------------------------------------------------------------------------
# Modal submit handler (deny with reason)
# ---------------------------------------------------------------------------

def _handle_modal(interaction: dict, *, service) -> dict:
    data = interaction.get("data") or {}
    custom_id: str = data.get("custom_id") or ""
    action, _, arg = custom_id.partition(":")

    if action == "deny_confirm" and arg:
        reason = _extract_modal_text(data, input_id="reason") or None
        try:
            service.deny(arg, decision_note=reason)
            title = f"❌ Denied `{arg}`"
            if reason:
                title += f" — {reason[:120]}"
            return _render_resolved(title=title, color=COLOR_DANGER, update=True)
        except Exception as exc:
            return _ephemeral(f"⚠️ Could not deny `{arg}`: {exc}")

    return _ephemeral(f"Unknown modal `{custom_id}`.")


def _extract_modal_text(data: dict, *, input_id: str) -> Optional[str]:
    """Walk a modal submission payload and return the first text value for input_id."""
    for row in data.get("components", []) or []:
        for comp in row.get("components", []) or []:
            if comp.get("custom_id") == input_id:
                value = comp.get("value")
                if isinstance(value, str):
                    return value.strip()
    return None


# ---------------------------------------------------------------------------
# Panel renderers
# ---------------------------------------------------------------------------

def _panel_main(service, *, update: bool = False, ephemeral: bool = False) -> dict:
    """Main dashboard panel with counts + navigation buttons."""
    today = service.recap_today()
    pending = today.get("pending_approvals_count", 0)
    in_progress = today.get("in_progress_count", 0)
    overdue = today.get("overdue_count", 0)
    counts = today.get("counts", {}) or {}

    embed = _base_embed(
        title="agentic-os control panel",
        description=(
            f"• Pending approvals: **{pending}**\n"
            f"• In progress: **{in_progress}**\n"
            f"• Overdue: **{overdue}**\n"
            f"• Today: done **{counts.get('done', 0)}**, "
            f"in-progress **{counts.get('in_progress', 0)}**, "
            f"to-do **{counts.get('to_do', 0)}**"
        ),
        color=COLOR_INFO,
    )
    embed["url"] = _public_url("/")

    components = [
        _row([
            _button("Pending approvals", f"panel:pending", BTN_PRIMARY, emoji="⏳"),
            _button("In progress", f"panel:in_progress", BTN_SECONDARY, emoji="⚙️"),
            _button("Overdue", f"panel:overdue", BTN_SECONDARY, emoji="🕐"),
        ]),
        _row([
            _button("Recent failures", f"panel:failures", BTN_SECONDARY, emoji="❌"),
            _button("Status", f"panel:status", BTN_SECONDARY, emoji="📊"),
            _button("Refresh", f"panel:home", BTN_SECONDARY, emoji="🔄"),
        ]),
        _row([
            _link_button("Open dashboard", _public_url("/")),
            _link_button("Approvals", _public_url("/approvals")),
            _link_button("Audit", _public_url("/audit")),
        ]),
    ]

    return _response(embeds=[embed], components=components, update=update, ephemeral=ephemeral)


def _panel_pending(service, *, update: bool = False) -> dict:
    approvals_recap = service.recap_approvals()
    records = approvals_recap.get("records", []) or []

    if not records:
        embed = _base_embed(
            title="No pending approvals",
            description="Nothing waiting for your decision right now.",
            color=COLOR_SUCCESS,
        )
        return _response(
            embeds=[embed],
            components=[_nav_row("pending")],
            update=update,
        )

    # Discord hard-limits messages to 5 action rows total. We reserve one row
    # for the nav (Back/Refresh), leaving 4 rows for approval actions.
    MAX_APPROVAL_ROWS = 4

    embed = _base_embed(
        title=f"Pending approvals ({len(records)})",
        description="Click a button below to approve or deny. Denying opens a modal for an optional reason.",
        color=COLOR_PENDING,
    )
    fields: list[dict] = []
    component_rows: list[dict] = []

    for idx, rec in enumerate(records[:MAX_APPROVAL_ROWS]):
        approval_id = rec["approval_id"]
        task_id = rec["task_id"]
        hours = rec.get("hours_pending", 0.0)
        title = (rec.get("user_request") or "").splitlines()[0].strip() or task_id
        title = title[:90]
        fields.append({
            "name": f"{idx + 1}. {title}",
            "value": (
                f"`{approval_id}` · task `{task_id}` · {hours:.1f}h pending\n"
                f"domain `{rec.get('domain', '?')}` · target `{rec.get('target') or '—'}` · "
                f"op `{rec.get('operation_key') or '—'}`"
            )[:1024],
            "inline": False,
        })
        component_rows.append(_row([
            _button(f"Approve #{idx + 1}", f"approve:{approval_id}", BTN_SUCCESS, emoji="✅"),
            _button(f"Deny #{idx + 1}", f"deny:{approval_id}", BTN_DANGER, emoji="✖️"),
            _link_button(f"Open #{idx + 1}", _public_url(f"/approvals/{approval_id}")),
        ]))

    embed["fields"] = fields
    component_rows.append(_nav_row("pending"))

    if len(records) > MAX_APPROVAL_ROWS:
        embed["footer"] = {
            "text": (
                f"Showing {MAX_APPROVAL_ROWS} of {len(records)}. "
                "Use the dashboard for the rest."
            )
        }

    return _response(embeds=[embed], components=component_rows, update=update)


def _panel_in_progress(service, *, update: bool = False) -> dict:
    recap = service.recap_in_progress()
    records = recap.get("records", []) or []
    embed = _base_embed(
        title=f"In-progress tasks ({len(records)})",
        description="Tasks currently active in the system.",
        color=COLOR_INFO,
    )
    embed["fields"] = _task_fields(records, limit=8)
    rows = [_nav_row("in_progress")]
    return _response(embeds=[embed], components=rows, update=update)


def _panel_failures(service, *, update: bool = False) -> dict:
    recap = service.recap_failures(limit=8)
    records = recap.get("records", []) or []
    embed = _base_embed(
        title=f"Recent failures ({len(records)})",
        description="Most recent terminal tasks (review for regressions).",
        color=COLOR_DANGER,
    )
    embed["fields"] = _task_fields(records, limit=8)
    rows = [_nav_row("failures")]
    return _response(embeds=[embed], components=rows, update=update)


def _panel_overdue(service, *, update: bool = False) -> dict:
    recap = service.recap_overdue(threshold_hours=48.0)
    records = recap.get("records", []) or []
    embed = _base_embed(
        title=f"Overdue tasks ({len(records)})",
        description="Tasks with no update for ≥48h.",
        color=COLOR_DANGER if records else COLOR_SUCCESS,
    )
    embed["fields"] = _task_fields(records, limit=8)
    rows = [_nav_row("overdue")]
    return _response(embeds=[embed], components=rows, update=update)


def _panel_status(service, *, update: bool = False) -> dict:
    from .health import get_system_health

    health = get_system_health(service)
    status = health.get("status", "unknown")
    color = {
        "ok": COLOR_SUCCESS,
        "degraded": COLOR_PENDING,
        "error": COLOR_DANGER,
    }.get(status, COLOR_MUTED)

    db_state = "ok" if health.get("db", {}).get("reachable") else "unreachable"
    jobs = health.get("cron", {}).get("jobs", []) or []
    erroring = [j for j in jobs if j.get("status") == "error"]

    description_lines = [
        f"Overall: **{status}**",
        f"DB: `{db_state}`",
        f"Scheduler jobs: {len(jobs)} registered, {len(erroring)} erroring",
    ]
    if erroring:
        description_lines.append("")
        description_lines.append("Erroring jobs:")
        for j in erroring[:5]:
            description_lines.append(f"• `{j.get('id', '?')}` — {j.get('last_error', '?')}")

    embed = _base_embed(
        title="System status",
        description="\n".join(description_lines),
        color=color,
    )
    rows = [_nav_row("status")]
    return _response(embeds=[embed], components=rows, update=update)


# ---------------------------------------------------------------------------
# Approval-request notification (buttoned) — called from notification_router
# ---------------------------------------------------------------------------

def approval_request_payload(
    *,
    task,
    approval,
) -> dict:
    """
    Build a JSON body (embeds + components) for an approval-request DM.

    This is a message payload (not an interaction response), so it's the shape
    posted to POST /channels/{id}/messages. Returns a dict that the notifier
    will json.dumps() before sending.
    """
    title = (task.user_request.splitlines()[0].strip() or task.id)[:100]
    embed = _base_embed(
        title=f"⏳ Approval required: {title}",
        description=(
            f"**Task** `{task.id}`\n"
            f"**Approval** `{approval.id}`\n"
            f"**Domain** `{task.domain}` · **Risk** `{task.risk_level}`\n"
            f"**Operation** `{approval.operation_key or '—'}`"
        ),
        color=COLOR_PENDING,
    )
    embed["url"] = _public_url(f"/approvals/{approval.id}")
    embed["footer"] = {
        "text": "Approve · Deny (with reason) · Open in dashboard",
    }

    components = [
        _row([
            _button("Approve", f"approve:{approval.id}", BTN_SUCCESS, emoji="✅"),
            _button("Deny", f"deny:{approval.id}", BTN_DANGER, emoji="✖️"),
            _link_button("Open", _public_url(f"/approvals/{approval.id}")),
        ]),
    ]
    return {"embeds": [embed], "components": components}


# ---------------------------------------------------------------------------
# Discord payload builders
# ---------------------------------------------------------------------------

def _response(
    *,
    embeds: Optional[list] = None,
    components: Optional[list] = None,
    content: Optional[str] = None,
    update: bool = False,
    ephemeral: bool = False,
) -> dict:
    """Wrap data into an interaction response dict."""
    rtype = RESP_UPDATE_MESSAGE if update else RESP_CHANNEL_MESSAGE
    data: dict[str, Any] = {}
    if content is not None:
        data["content"] = content
    if embeds is not None:
        data["embeds"] = embeds
    if components is not None:
        data["components"] = components
    if ephemeral and not update:
        data["flags"] = FLAG_EPHEMERAL
    return {"type": rtype, "data": data}


def _ephemeral(message: str) -> dict:
    return {
        "type": RESP_CHANNEL_MESSAGE,
        "data": {"content": message, "flags": FLAG_EPHEMERAL},
    }


def _render_resolved(*, title: str, color: int, update: bool) -> dict:
    embed = _base_embed(title=title, description="", color=color)
    return _response(embeds=[embed], components=[], update=update)


def _deny_modal(approval_id: str) -> dict:
    """Return an interaction response that opens a modal asking for a deny reason."""
    return {
        "type": RESP_MODAL,
        "data": {
            "custom_id": f"deny_confirm:{approval_id}",
            "title": f"Deny {approval_id[:32]}",
            "components": [
                {
                    "type": COMPONENT_ACTION_ROW,
                    "components": [
                        {
                            "type": COMPONENT_TEXT_INPUT,
                            "custom_id": "reason",
                            "label": "Reason (optional)",
                            "style": 2,  # paragraph
                            "required": False,
                            "max_length": 500,
                            "placeholder": "Why deny this approval?",
                        }
                    ],
                }
            ],
        },
    }


def _base_embed(*, title: str, description: str, color: int) -> dict:
    embed: dict[str, Any] = {"title": title[:256], "color": color}
    if description:
        embed["description"] = description[:4096]
    return embed


def _row(buttons: Iterable[dict]) -> dict:
    return {"type": COMPONENT_ACTION_ROW, "components": list(buttons)}


def _button(label: str, custom_id: str, style: int, *, emoji: Optional[str] = None) -> dict:
    btn: dict[str, Any] = {
        "type": COMPONENT_BUTTON,
        "style": style,
        "label": label[:80],
        "custom_id": custom_id[:100],
    }
    if emoji:
        btn["emoji"] = {"name": emoji}
    return btn


def _link_button(label: str, url: str) -> dict:
    return {
        "type": COMPONENT_BUTTON,
        "style": BTN_LINK,
        "label": label[:80],
        "url": url,
    }


def _nav_row(view: str) -> dict:
    return _row([
        _button("◀ Back", "panel:home", BTN_SECONDARY),
        _button("🔄 Refresh", f"panel:{view}", BTN_PRIMARY),
    ])


def _task_fields(records: list[dict], *, limit: int) -> list[dict]:
    fields = []
    for idx, rec in enumerate(records[:limit]):
        task_id = rec.get("task_id") or rec.get("id") or "?"
        title = (rec.get("user_request") or rec.get("result_summary") or "").splitlines()
        title = (title[0].strip() if title else "") or task_id
        fields.append({
            "name": f"{idx + 1}. {title[:90]}",
            "value": (
                f"`{task_id}` · status `{rec.get('status', '?')}` · "
                f"domain `{rec.get('domain', '?')}` · target `{rec.get('target') or '—'}`"
            )[:1024],
            "inline": False,
        })
    if not fields:
        fields = [{"name": "(nothing)", "value": "No records.", "inline": False}]
    return fields


def _public_url(path: str) -> str:
    base = os.environ.get("AGENTIC_OS_PUBLIC_URL", "").rstrip("/")
    if not base:
        return path
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"
