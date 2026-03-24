from __future__ import annotations

from typing import Any, Optional

from .daily_routine import DEFAULT_DELIVERY_TIME, DEFAULT_RECIPIENT, DEFAULT_TIMEZONE


def normalize_openclaw_daily_routine_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("openclaw daily-routine payload must be a JSON object")
    result = {
        "date": str(payload.get("date")) if payload.get("date") else None,
        "timezone": str(payload.get("timezone") or payload.get("timezone_name") or DEFAULT_TIMEZONE),
        "recipient": str(payload.get("recipient") or DEFAULT_RECIPIENT),
        "delivery_time": str(payload.get("delivery_time") or payload.get("deliveryTime") or DEFAULT_DELIVERY_TIME),
        "calendar": normalize_calendar_summary(
            _pick_dict(payload, "calendar", "calendar_summary", "calendarSummary")
        ),
        "personal_inbox": normalize_inbox_summary(
            _pick_dict(payload, "personal_inbox", "personalInbox", "personal_inbox_summary", "personalInboxSummary"),
            mailbox_kind="personal",
        ),
        "agent_inbox": normalize_inbox_summary(
            _pick_dict(payload, "agent_inbox", "agentInbox", "agent_inbox_summary", "agentInboxSummary"),
            mailbox_kind="agent",
        ),
        "notion": normalize_notion_summary(
            _pick_dict(payload, "notion", "notion_summary", "notionSummary")
        ),
    }
    if result["date"] is None:
        result.pop("date")
    return result


def normalize_calendar_summary(section: Optional[dict[str, Any]]) -> dict[str, Any]:
    value = _unwrap_tool_result(section)
    events_raw = _pick_list(value, "events", "calendar_events", "calendarEvents", "items", "entries")
    constraints = _string_list(_pick_list(value, "constraints", "prep", "notes", "highlights"))
    events = [normalize_calendar_event(item) for item in events_raw if isinstance(item, dict)]
    return {"events": events, "constraints": constraints}


def normalize_calendar_event(item: dict[str, Any]) -> dict[str, Any]:
    title = _first_non_empty(
        item,
        "title",
        "summary",
        "name",
        "subject",
        fallback="Untitled event",
    )
    summary = _optional_text(item.get("summary") or item.get("description") or item.get("snippet"))
    prep_needed = _optional_text(
        item.get("prep_needed")
        or item.get("prepNeeded")
        or item.get("required_prep")
        or item.get("requiredPrep")
        or item.get("prep")
    )
    actionable = _coerce_bool(item.get("actionable"), default=bool(prep_needed))
    return {
        "title": title,
        "start": _optional_text(item.get("start") or item.get("start_time") or item.get("startTime")),
        "end": _optional_text(item.get("end") or item.get("end_time") or item.get("endTime")),
        "location": _optional_text(item.get("location")),
        "summary": summary,
        "prep_needed": prep_needed,
        "actionable": actionable,
    }


def normalize_inbox_summary(section: Optional[dict[str, Any]], *, mailbox_kind: str) -> dict[str, Any]:
    value = _unwrap_tool_result(section)
    summary = {
        "urgent": [normalize_inbox_item(item) for item in _pick_list(value, "urgent") if isinstance(item, dict)],
        "needs_reply": [
            normalize_inbox_item(item)
            for item in _pick_list(value, "needs_reply", "needsReply")
            if isinstance(item, dict)
        ],
        "important_fyi": [
            normalize_inbox_item(item)
            for item in _pick_list(value, "important_fyi", "importantFYI")
            if isinstance(item, dict)
        ],
        "operational_items": [
            normalize_inbox_item(item)
            for item in _pick_list(value, "operational_items", "operationalItems")
            if isinstance(item, dict)
        ],
        "alerts": [normalize_inbox_item(item) for item in _pick_list(value, "alerts") if isinstance(item, dict)],
    }
    has_grouped_keys = any(summary[group] for group in summary)
    if has_grouped_keys:
        return summary

    items = _pick_list(value, "items", "messages", "threads", "emails", "entries", "results")
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        normalized = normalize_inbox_item(raw_item)
        bucket = classify_inbox_bucket(raw_item, mailbox_kind=mailbox_kind, fallback_item=normalized)
        summary[bucket].append(normalized)
    return summary


def normalize_inbox_item(item: dict[str, Any]) -> dict[str, Any]:
    requested_action = _optional_text(
        item.get("requested_action")
        or item.get("requestedAction")
        or item.get("next_action")
        or item.get("nextAction")
        or item.get("action")
    )
    due = _optional_text(item.get("due") or item.get("deadline") or item.get("due_at") or item.get("dueAt"))
    actionable = _coerce_bool(item.get("actionable"), default=bool(requested_action or due))
    message_id = _optional_text(item.get("message_id") or item.get("messageId") or item.get("id"))
    return {
        "subject": _first_non_empty(item, "subject", "title", "name", fallback="Untitled message"),
        "sender": _optional_text(item.get("sender") or item.get("from") or item.get("author")),
        "summary": _optional_text(item.get("summary") or item.get("snippet") or item.get("preview")),
        "requested_action": requested_action,
        "due": due,
        "actionable": actionable,
        "message_id": message_id,
    }


def classify_inbox_bucket(
    item: dict[str, Any],
    *,
    mailbox_kind: str,
    fallback_item: Optional[dict[str, Any]] = None,
) -> str:
    raw_bucket = _normalized_label(
        item.get("bucket")
        or item.get("category")
        or item.get("section")
        or item.get("label")
        or item.get("priority")
    )
    bucket_map = _personal_bucket_map() if mailbox_kind == "personal" else _agent_bucket_map()
    if raw_bucket in bucket_map:
        return bucket_map[raw_bucket]
    actionable = _coerce_bool(item.get("actionable"), default=False)
    if fallback_item is not None and fallback_item.get("actionable"):
        actionable = True
    if mailbox_kind == "personal":
        return "needs_reply" if actionable else "important_fyi"
    return "operational_items" if actionable else "alerts"


def normalize_notion_summary(section: Optional[dict[str, Any]]) -> dict[str, Any]:
    value = _unwrap_tool_result(section)
    summary = {
        "inbox": [normalize_notion_item(item) for item in _pick_list(value, "inbox", "Inbox") if isinstance(item, dict)],
        "review": [normalize_notion_item(item) for item in _pick_list(value, "review", "Review") if isinstance(item, dict)],
        "planned": [
            normalize_notion_item(item)
            for item in _pick_list(value, "planned", "Planned")
            if isinstance(item, dict)
        ],
        "blocked": [
            normalize_notion_item(item)
            for item in _pick_list(value, "blocked", "Blocked")
            if isinstance(item, dict)
        ],
        "stale": [normalize_notion_item(item) for item in _pick_list(value, "stale", "Stale") if isinstance(item, dict)],
    }
    has_grouped_keys = any(summary[group] for group in summary)
    if has_grouped_keys:
        return summary

    items = _pick_list(value, "items", "tasks", "pages", "entries", "results")
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized = normalize_notion_item(item)
        bucket = classify_notion_bucket(item, fallback_item=normalized)
        if bucket == "stale":
            normalized["stale"] = True
        summary[bucket].append(normalized)
    return summary


def normalize_notion_item(item: dict[str, Any]) -> dict[str, Any]:
    blocked_reason = _optional_text(item.get("blocked_reason") or item.get("blockedReason") or item.get("reason"))
    next_step = _optional_text(item.get("next_step") or item.get("nextStep") or item.get("action"))
    stale = _coerce_bool(item.get("stale"), default=False)
    actionable = _coerce_bool(item.get("actionable"), default=bool(next_step or blocked_reason or stale))
    return {
        "title": _first_non_empty(item, "title", "name", fallback="Untitled task"),
        "status": _optional_text(item.get("status")),
        "area": _optional_text(item.get("area") or item.get("domain")),
        "summary": _optional_text(item.get("summary") or item.get("description") or item.get("snippet")),
        "next_step": next_step,
        "blocked_reason": blocked_reason,
        "stale": stale,
        "actionable": actionable,
    }


def classify_notion_bucket(item: dict[str, Any], *, fallback_item: Optional[dict[str, Any]] = None) -> str:
    status = _normalized_label(item.get("status"))
    if fallback_item is not None and fallback_item.get("stale"):
        return "stale"
    if status in {"blocked"}:
        return "blocked"
    if status in {"stale", "waiting", "on_hold"}:
        return "stale"
    if status in {"review", "needs_review", "needsreview"}:
        return "review"
    if status in {"inbox", "new"}:
        return "inbox"
    return "planned"


def _unwrap_tool_result(section: Optional[dict[str, Any]]) -> dict[str, Any]:
    value = section or {}
    if not isinstance(value, dict):
        return {}
    unwrapped = value
    for key in ("tool_result", "toolResult", "result", "data", "payload"):
        nested = unwrapped.get(key)
        if isinstance(nested, dict) and len(unwrapped) == 1:
            unwrapped = nested
        else:
            break
    return unwrapped


def _pick_dict(payload: dict[str, Any], *keys: str) -> Optional[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return None


def _pick_list(payload: Optional[dict[str, Any]], *keys: str) -> list[Any]:
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _first_non_empty(payload: dict[str, Any], *keys: str, fallback: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return fallback


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_list(items: list[Any]) -> list[str]:
    output = []
    for item in items:
        text = _optional_text(item)
        if text:
            output.append(text)
    return output


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _normalized_label(value: Any) -> str:
    text = _optional_text(value)
    if text is None:
        return ""
    return text.lower().replace("-", "_").replace(" ", "_")


def _personal_bucket_map() -> dict[str, str]:
    return {
        "urgent": "urgent",
        "high": "urgent",
        "critical": "urgent",
        "asap": "urgent",
        "needs_reply": "needs_reply",
        "needsreply": "needs_reply",
        "reply": "needs_reply",
        "action": "needs_reply",
        "action_required": "needs_reply",
        "follow_up": "needs_reply",
        "fyi": "important_fyi",
        "important_fyi": "important_fyi",
        "importantfyi": "important_fyi",
        "info": "important_fyi",
    }


def _agent_bucket_map() -> dict[str, str]:
    return {
        "alerts": "alerts",
        "alert": "alerts",
        "warning": "alerts",
        "incident": "alerts",
        "operational_items": "operational_items",
        "operationalitems": "operational_items",
        "operational": "operational_items",
        "ops": "operational_items",
        "action": "operational_items",
        "task": "operational_items",
        "intake": "operational_items",
    }
