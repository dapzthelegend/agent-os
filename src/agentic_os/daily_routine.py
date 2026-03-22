from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional


DEFAULT_RECIPIENT = "franchieinc@gmail.com"
DEFAULT_DELIVERY_TIME = "08:30"
DEFAULT_TIMEZONE = "Europe/London"


def _today_in_timezone(timezone_name: str) -> date:
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover
        return datetime.utcnow().date()
    return datetime.now(ZoneInfo(timezone_name)).date()


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _coerce_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("expected a list value")
    return value


def _string_list(items: Any) -> list[str]:
    return [str(item).strip() for item in _coerce_list(items) if str(item).strip()]


def _slugify(value: str) -> str:
    cleaned = []
    previous_dash = False
    for char in value.lower():
        if char.isalnum():
            cleaned.append(char)
            previous_dash = False
            continue
        if not previous_dash:
            cleaned.append("-")
            previous_dash = True
    slug = "".join(cleaned).strip("-")
    return slug or "item"


def _compact_text(value: Optional[str], *, fallback: str) -> str:
    if value is None:
        return fallback
    normalized = " ".join(value.split())
    return normalized or fallback


@dataclass(frozen=True)
class CalendarEvent:
    title: str
    start: Optional[str] = None
    end: Optional[str] = None
    location: Optional[str] = None
    summary: Optional[str] = None
    prep_needed: Optional[str] = None
    actionable: bool = False

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CalendarEvent":
        return cls(
            title=str(payload.get("title") or "Untitled event"),
            start=payload.get("start"),
            end=payload.get("end"),
            location=payload.get("location"),
            summary=payload.get("summary"),
            prep_needed=payload.get("prep_needed") or payload.get("prepNeeded"),
            actionable=_coerce_bool(payload.get("actionable"), default=False),
        )

    def line(self) -> str:
        parts = [self.title]
        if self.start:
            parts.append(f"@ {self.start}")
        if self.location:
            parts.append(f"({self.location})")
        if self.summary:
            parts.append(f"- {self.summary}")
        return " ".join(parts)


@dataclass(frozen=True)
class InboxItem:
    subject: str
    sender: Optional[str] = None
    summary: Optional[str] = None
    requested_action: Optional[str] = None
    due: Optional[str] = None
    actionable: bool = False

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "InboxItem":
        return cls(
            subject=str(payload.get("subject") or "Untitled message"),
            sender=payload.get("sender"),
            summary=payload.get("summary"),
            requested_action=payload.get("requested_action") or payload.get("requestedAction"),
            due=payload.get("due"),
            actionable=_coerce_bool(payload.get("actionable"), default=False),
        )

    def line(self) -> str:
        parts = [self.subject]
        if self.sender:
            parts.append(f"from {self.sender}")
        if self.summary:
            parts.append(f"- {self.summary}")
        if self.due:
            parts.append(f"(due {self.due})")
        return " ".join(parts)


@dataclass(frozen=True)
class NotionTaskSummary:
    title: str
    status: Optional[str] = None
    area: Optional[str] = None
    summary: Optional[str] = None
    next_step: Optional[str] = None
    blocked_reason: Optional[str] = None
    stale: bool = False
    actionable: bool = False

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "NotionTaskSummary":
        return cls(
            title=str(payload.get("title") or "Untitled task"),
            status=payload.get("status"),
            area=payload.get("area"),
            summary=payload.get("summary"),
            next_step=payload.get("next_step") or payload.get("nextStep"),
            blocked_reason=payload.get("blocked_reason") or payload.get("blockedReason"),
            stale=_coerce_bool(payload.get("stale"), default=False),
            actionable=_coerce_bool(payload.get("actionable"), default=False),
        )

    def line(self) -> str:
        parts = [self.title]
        if self.summary:
            parts.append(f"- {self.summary}")
        if self.next_step:
            parts.append(f"(next: {self.next_step})")
        if self.blocked_reason:
            parts.append(f"(blocked: {self.blocked_reason})")
        return " ".join(parts)


@dataclass(frozen=True)
class CalendarSummary:
    events: list[CalendarEvent]
    constraints: list[str]

    @classmethod
    def from_dict(cls, payload: Optional[dict[str, Any]]) -> "CalendarSummary":
        value = payload or {}
        return cls(
            events=[CalendarEvent.from_dict(item) for item in _coerce_list(value.get("events"))],
            constraints=_string_list(value.get("constraints")),
        )


@dataclass(frozen=True)
class InboxSummary:
    urgent: list[InboxItem]
    needs_reply: list[InboxItem]
    important_fyi: list[InboxItem]
    operational_items: list[InboxItem]
    alerts: list[InboxItem]

    @classmethod
    def from_dict(cls, payload: Optional[dict[str, Any]]) -> "InboxSummary":
        value = payload or {}
        return cls(
            urgent=[InboxItem.from_dict(item) for item in _coerce_list(value.get("urgent"))],
            needs_reply=[InboxItem.from_dict(item) for item in _coerce_list(value.get("needs_reply") or value.get("needsReply"))],
            important_fyi=[InboxItem.from_dict(item) for item in _coerce_list(value.get("important_fyi") or value.get("importantFYI"))],
            operational_items=[InboxItem.from_dict(item) for item in _coerce_list(value.get("operational_items") or value.get("operationalItems"))],
            alerts=[InboxItem.from_dict(item) for item in _coerce_list(value.get("alerts"))],
        )


@dataclass(frozen=True)
class NotionSummary:
    inbox: list[NotionTaskSummary]
    review: list[NotionTaskSummary]
    planned: list[NotionTaskSummary]
    blocked: list[NotionTaskSummary]
    stale: list[NotionTaskSummary]

    @classmethod
    def from_dict(cls, payload: Optional[dict[str, Any]]) -> "NotionSummary":
        value = payload or {}
        return cls(
            inbox=[NotionTaskSummary.from_dict(item) for item in _coerce_list(value.get("inbox") or value.get("Inbox"))],
            review=[NotionTaskSummary.from_dict(item) for item in _coerce_list(value.get("review") or value.get("Review"))],
            planned=[NotionTaskSummary.from_dict(item) for item in _coerce_list(value.get("planned") or value.get("Planned"))],
            blocked=[NotionTaskSummary.from_dict(item) for item in _coerce_list(value.get("blocked") or value.get("Blocked"))],
            stale=[NotionTaskSummary.from_dict(item) for item in _coerce_list(value.get("stale") or value.get("Stale"))],
        )


@dataclass(frozen=True)
class DailyRoutineInput:
    date: str
    timezone: str
    recipient: str
    delivery_time: str
    calendar: CalendarSummary
    personal_inbox: InboxSummary
    agent_inbox: InboxSummary
    notion: NotionSummary

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DailyRoutineInput":
        timezone = str(payload.get("timezone") or DEFAULT_TIMEZONE)
        run_date = str(payload.get("date") or _today_in_timezone(timezone))
        return cls(
            date=run_date,
            timezone=timezone,
            recipient=str(payload.get("recipient") or DEFAULT_RECIPIENT),
            delivery_time=str(payload.get("delivery_time") or payload.get("deliveryTime") or DEFAULT_DELIVERY_TIME),
            calendar=CalendarSummary.from_dict(payload.get("calendar")),
            personal_inbox=InboxSummary.from_dict(payload.get("personal_inbox") or payload.get("personalInbox")),
            agent_inbox=InboxSummary.from_dict(payload.get("agent_inbox") or payload.get("agentInbox")),
            notion=NotionSummary.from_dict(payload.get("notion")),
        )

    @property
    def yesterday(self) -> str:
        return (date.fromisoformat(self.date) - timedelta(days=1)).isoformat()


@dataclass(frozen=True)
class YesterdayRecap:
    completed: list[str]
    blocked: list[str]
    still_open: list[str]


@dataclass(frozen=True)
class TodayRecap:
    calendar_events: list[str]
    constraints: list[str]


@dataclass(frozen=True)
class InboxRecap:
    urgent: list[str]
    needs_reply: list[str]
    important_fyi: list[str]
    operational_items: list[str]
    alerts: list[str]


@dataclass(frozen=True)
class NotionRecap:
    inbox: list[str]
    review: list[str]
    planned: list[str]
    blocked_or_stale: list[str]


@dataclass(frozen=True)
class FollowUpAction:
    title: str
    summary: str
    domain: str
    source_kind: str
    source_title: str
    operation_key: str
    notion_title: Optional[str]
    rationale: str


@dataclass(frozen=True)
class DailyRecap:
    run_date: str
    recipient: str
    delivery_time: str
    timezone: str
    yesterday: YesterdayRecap
    today: TodayRecap
    personal_inbox: InboxRecap
    agent_inbox: InboxRecap
    notion: NotionRecap
    recommended_next_actions: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def infer_domain(source_kind: str, area: Optional[str] = None) -> str:
    if area in {"personal", "technical", "finance", "system"}:
        return area
    if source_kind == "personal_inbox":
        return "personal"
    if source_kind == "calendar":
        return "personal"
    if source_kind == "agent_inbox":
        return "system"
    return "technical"


def summarize_task_for_yesterday(user_request: str, result_summary: Optional[str]) -> str:
    if result_summary:
        return _compact_text(f"{user_request} -> {result_summary}", fallback=user_request)
    return _compact_text(user_request, fallback="Unnamed task")


def extract_follow_up_actions(payload: DailyRoutineInput, *, limit: int = 5) -> list[FollowUpAction]:
    actions: list[FollowUpAction] = []

    for event in payload.calendar.events:
        if event.prep_needed:
            title = f"Prepare for {event.title}"
            actions.append(
                FollowUpAction(
                    title=title,
                    summary=_compact_text(event.prep_needed, fallback=title),
                    domain=infer_domain("calendar"),
                    source_kind="calendar",
                    source_title=event.title,
                    operation_key=f"daily-routine-{payload.date}-calendar-{_slugify(event.title)}",
                    notion_title=title,
                    rationale="Calendar prep needed",
                )
            )
        elif event.actionable:
            title = f"Follow up on {event.title}"
            actions.append(
                FollowUpAction(
                    title=title,
                    summary=_compact_text(event.summary, fallback=title),
                    domain=infer_domain("calendar"),
                    source_kind="calendar",
                    source_title=event.title,
                    operation_key=f"daily-routine-{payload.date}-calendar-{_slugify(event.title)}",
                    notion_title=title,
                    rationale="Calendar event marked actionable",
                )
            )

    for item in payload.personal_inbox.urgent + payload.personal_inbox.needs_reply:
        if not (item.actionable or item.requested_action or item.due):
            continue
        title = item.requested_action or f"Reply on {item.subject}"
        actions.append(
            FollowUpAction(
                title=_compact_text(title, fallback=item.subject),
                summary=_compact_text(item.summary or item.requested_action, fallback=item.subject),
                domain=infer_domain("personal_inbox"),
                source_kind="personal_inbox",
                source_title=item.subject,
                operation_key=f"daily-routine-{payload.date}-personal-{_slugify(item.subject)}",
                notion_title=item.subject,
                rationale="Personal inbox item needs action",
            )
        )

    for item in payload.agent_inbox.operational_items + payload.agent_inbox.alerts:
        if not (item.actionable or item.requested_action or item.due):
            continue
        title = item.requested_action or f"Handle {item.subject}"
        actions.append(
            FollowUpAction(
                title=_compact_text(title, fallback=item.subject),
                summary=_compact_text(item.summary or item.requested_action, fallback=item.subject),
                domain=infer_domain("agent_inbox"),
                source_kind="agent_inbox",
                source_title=item.subject,
                operation_key=f"daily-routine-{payload.date}-agent-{_slugify(item.subject)}",
                notion_title=item.subject,
                rationale="Agent inbox operational follow-up",
            )
        )

    for item in payload.notion.blocked + payload.notion.stale:
        if not (item.blocked_reason or item.next_step or item.actionable or item.stale):
            continue
        title = item.next_step or f"Unblock {item.title}"
        actions.append(
            FollowUpAction(
                title=_compact_text(title, fallback=item.title),
                summary=_compact_text(item.blocked_reason or item.summary or item.next_step, fallback=item.title),
                domain=infer_domain("notion", area=item.area),
                source_kind="notion",
                source_title=item.title,
                operation_key=f"daily-routine-{payload.date}-notion-{_slugify(item.title)}",
                notion_title=None,
                rationale="Blocked or stale Notion work needs movement",
            )
        )

    deduped: list[FollowUpAction] = []
    seen = set()
    for action in actions:
        if action.operation_key in seen:
            continue
        seen.add(action.operation_key)
        deduped.append(action)
        if len(deduped) >= limit:
            break
    return deduped


def build_daily_recap(payload: DailyRoutineInput, yesterday: YesterdayRecap) -> DailyRecap:
    actions = extract_follow_up_actions(payload)
    return DailyRecap(
        run_date=payload.date,
        recipient=payload.recipient,
        delivery_time=payload.delivery_time,
        timezone=payload.timezone,
        yesterday=yesterday,
        today=TodayRecap(
            calendar_events=[event.line() for event in payload.calendar.events],
            constraints=payload.calendar.constraints,
        ),
        personal_inbox=InboxRecap(
            urgent=[item.line() for item in payload.personal_inbox.urgent],
            needs_reply=[item.line() for item in payload.personal_inbox.needs_reply],
            important_fyi=[item.line() for item in payload.personal_inbox.important_fyi],
            operational_items=[],
            alerts=[],
        ),
        agent_inbox=InboxRecap(
            urgent=[],
            needs_reply=[],
            important_fyi=[],
            operational_items=[item.line() for item in payload.agent_inbox.operational_items],
            alerts=[item.line() for item in payload.agent_inbox.alerts],
        ),
        notion=NotionRecap(
            inbox=[item.line() for item in payload.notion.inbox],
            review=[item.line() for item in payload.notion.review],
            planned=[item.line() for item in payload.notion.planned],
            blocked_or_stale=[item.line() for item in (payload.notion.blocked + payload.notion.stale)],
        ),
        recommended_next_actions=[action.title for action in actions],
    )


def render_plaintext_email(recap: DailyRecap) -> str:
    sections = [
        f"Daily recap for {recap.run_date}",
        f"Recipient: {recap.recipient}",
        f"Scheduled send: {recap.delivery_time} {recap.timezone}",
        "",
        "Yesterday",
        _render_bullets("Completed", recap.yesterday.completed),
        _render_bullets("Blocked", recap.yesterday.blocked),
        _render_bullets("Still open", recap.yesterday.still_open),
        "",
        "Today",
        _render_bullets("Calendar events", recap.today.calendar_events),
        _render_bullets("Constraints / prep needed", recap.today.constraints),
        "",
        "Personal inbox",
        _render_bullets("Urgent", recap.personal_inbox.urgent),
        _render_bullets("Needs reply", recap.personal_inbox.needs_reply),
        _render_bullets("Important FYI", recap.personal_inbox.important_fyi),
        "",
        "Agent inbox",
        _render_bullets("Operational items", recap.agent_inbox.operational_items),
        _render_bullets("Alerts / automation intake", recap.agent_inbox.alerts),
        "",
        "Notion",
        _render_bullets("Inbox", recap.notion.inbox),
        _render_bullets("Review", recap.notion.review),
        _render_bullets("Planned", recap.notion.planned),
        _render_bullets("Blocked / stale", recap.notion.blocked_or_stale),
        "",
        "Recommended next actions",
        _render_list(recap.recommended_next_actions),
    ]
    return "\n".join(sections).strip()


def prepare_email_payload(recap: DailyRecap, body: str) -> dict[str, str]:
    return {
        "to": recap.recipient,
        "subject": f"Daily recap - {recap.run_date}",
        "body_text": body,
    }


def _render_bullets(title: str, items: list[str]) -> str:
    return f"{title}:\n{_render_list(items)}"


def _render_list(items: list[str]) -> str:
    if not items:
        return "- None"
    return "\n".join(f"- {item}" for item in items)
