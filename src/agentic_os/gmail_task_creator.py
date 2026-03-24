from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Paths
from .models import RequestClassification
from .service import AgenticOSService


DEFAULT_EMAIL_ROUTING = {
    "auto_draft_domains": [],
    "skip_senders": ["noreply@", "no-reply@", "notifications@"],
    "always_needs_approval_senders": [],
    "max_tasks_per_run": 10,
}


def create_tasks_from_inbox(
    inbox_summary: dict[str, Any],
    mailbox_kind: str,
    *,
    paths: Paths,
) -> list[str]:
    """Create backend tasks from a normalized inbox summary."""
    if mailbox_kind not in {"personal", "agent"}:
        raise ValueError("mailbox_kind must be 'personal' or 'agent'")
    if not isinstance(inbox_summary, dict):
        return []

    routing = _load_email_routing(paths)
    max_tasks = _safe_max_tasks(routing.get("max_tasks_per_run"))
    skip_senders = _normalized_patterns(routing.get("skip_senders"))

    service = AgenticOSService(paths)
    service.initialize()

    created_task_ids: list[str] = []
    seen_operation_keys: set[str] = set()
    for category, item in _iter_actionable_items(inbox_summary=inbox_summary, mailbox_kind=mailbox_kind):
        if max_tasks and len(created_task_ids) >= max_tasks:
            break

        operation_key = _operation_key_from_item(item)
        if operation_key is None or operation_key in seen_operation_keys:
            continue
        seen_operation_keys.add(operation_key)

        external_ref = operation_key
        if service.db.get_task_by_operation_key(operation_key) is not None:
            continue
        if service.db.get_task_by_external_ref(external_ref) is not None:
            continue

        subject = _clean_text(item.get("subject"))
        sender = _clean_text(item.get("sender"))
        if not subject:
            continue
        if sender and _matches_any_pattern(sender, skip_senders):
            continue
        if item.get("actionable") is False:
            continue

        create_payload = _build_task_payload(
            item=item,
            mailbox_kind=mailbox_kind,
            category=category,
            subject=subject,
            sender=sender,
            operation_key=operation_key,
            external_ref=external_ref,
        )
        if create_payload is None:
            continue
        try:
            result = service.create_request(**create_payload)
        except ValueError:
            # Covers races/duplicate operation keys if another worker inserted first.
            continue
        created_task_ids.append(result["task"].id)
    return created_task_ids


def _iter_actionable_items(*, inbox_summary: dict[str, Any], mailbox_kind: str) -> list[tuple[str, dict[str, Any]]]:
    if mailbox_kind == "personal":
        categories = ("urgent", "needs_reply")
    else:
        categories = ("operational_items", "alerts")
    items: list[tuple[str, dict[str, Any]]] = []
    for category in categories:
        for raw_item in inbox_summary.get(category, []):
            if isinstance(raw_item, dict):
                items.append((category, raw_item))
    return items


def _build_task_payload(
    *,
    item: dict[str, Any],
    mailbox_kind: str,
    category: str,
    subject: str,
    sender: str | None,
    operation_key: str,
    external_ref: str,
) -> dict[str, Any] | None:
    summary = _clean_text(item.get("summary"))
    requested_action = _clean_text(item.get("requested_action"))
    due = _clean_text(item.get("due"))

    metadata = {
        "channel": "gmail",
        "mailbox_kind": mailbox_kind,
        "category": category,
        "message_id": operation_key.removeprefix("gmail:"),
        "subject": subject,
        "sender": sender,
        "summary": summary,
        "requested_action": requested_action,
        "due": due,
    }

    if mailbox_kind == "personal":
        task_title = f"Reply to: {subject} (from {sender or 'Unknown sender'})"
        classification = RequestClassification(
            domain="personal",
            intent_type="draft",
            risk_level="medium",
        ).validate()
        return {
            "user_request": _build_user_request(task_title=task_title, sender=sender, summary=summary, due=due),
            "classification": classification,
            "target": "gmail_reply_draft",
            "request_metadata": metadata,
            "operation_key": operation_key,
            "external_ref": external_ref,
            "action_source": "openclaw_tool",
        }

    task_title = f"Handle: {subject}"
    if category == "alerts":
        classification = RequestClassification(
            domain="system",
            intent_type="capture",
            risk_level="low",
        ).validate()
        target = "gmail_alert"
    elif category == "operational_items":
        classification = RequestClassification(
            domain="system",
            intent_type="execute",
            risk_level="low",
        ).validate()
        target = "gmail_operational_item"
    else:
        return None

    return {
        "user_request": _build_user_request(
            task_title=task_title,
            sender=sender,
            summary=summary,
            requested_action=requested_action,
            due=due,
        ),
        "classification": classification,
        "target": target,
        "request_metadata": metadata,
        "operation_key": operation_key,
        "external_ref": external_ref,
        "action_source": "openclaw_tool",
    }


def _build_user_request(
    *,
    task_title: str,
    sender: str | None,
    summary: str | None,
    requested_action: str | None = None,
    due: str | None = None,
) -> str:
    lines = [task_title]
    if sender:
        lines.append(f"From: {sender}")
    if summary:
        lines.append(f"Summary: {summary}")
    if requested_action:
        lines.append(f"Requested action: {requested_action}")
    if due:
        lines.append(f"Due: {due}")
    return "\n".join(lines)


def _operation_key_from_item(item: dict[str, Any]) -> str | None:
    message_id = _clean_text(item.get("message_id") or item.get("messageId") or item.get("id"))
    if not message_id:
        return None
    return f"gmail:{message_id}"


def _load_email_routing(paths: Paths) -> dict[str, Any]:
    path = Path(paths.root) / "email_routing.json"
    if not path.exists():
        return dict(DEFAULT_EMAIL_ROUTING)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return dict(DEFAULT_EMAIL_ROUTING)
    merged = dict(DEFAULT_EMAIL_ROUTING)
    merged.update(payload)
    return merged


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalized_patterns(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip().lower() for item in value if str(item).strip()]


def _matches_any_pattern(sender: str, patterns: list[str]) -> bool:
    lowered = sender.lower()
    return any(pattern in lowered for pattern in patterns)


def _safe_max_tasks(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return int(DEFAULT_EMAIL_ROUTING["max_tasks_per_run"])
