#!/usr/bin/env python3
"""
OpenClaw daily routine runner.

Orchestrates the daily routine by:
1. Gathering context (calendar, inbox, Notion tasks)
2. Building a normalized payload
3. Calling the agentic-os daily routine service
4. Sending results (email, Notion, etc.)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional
from datetime import datetime, timezone


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _get_utc_now() -> str:
    """Get current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _build_daily_routine_payload(
    *,
    date: Optional[str] = None,
    timezone_name: Optional[str] = None,
    recipient: Optional[str] = None,
    delivery_time: Optional[str] = None,
    calendar: Optional[dict[str, Any]] = None,
    personal_inbox: Optional[dict[str, Any]] = None,
    agent_inbox: Optional[dict[str, Any]] = None,
    notion: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Build the payload for the daily routine engine.
    
    This includes all context needed for the daily routine: calendar, inbox,
    Notion tasks, etc.
    """
    payload: dict[str, Any] = {}
    
    if date:
        payload["date"] = date
    if timezone_name:
        payload["timezone"] = timezone_name
    if recipient:
        payload["recipient"] = recipient
    if delivery_time:
        payload["delivery_time"] = delivery_time
    
    if calendar:
        payload["calendar"] = calendar
    if personal_inbox:
        payload["personal_inbox"] = personal_inbox
    if agent_inbox:
        payload["agent_inbox"] = agent_inbox
    if notion:
        payload["notion"] = notion
    
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OpenClaw daily routine runner — orchestrates daily routine",
    )
    parser.add_argument(
        "--date",
        help="Date for the routine (ISO format, default: today)",
    )
    parser.add_argument(
        "--timezone",
        help="Timezone (default: UTC)",
    )
    parser.add_argument(
        "--recipient",
        help="Email recipient for daily routine email",
    )
    parser.add_argument(
        "--delivery-time",
        help="Time to deliver email (HH:MM, default: 08:30)",
    )
    parser.add_argument(
        "--calendar-input",
        help="Calendar context as JSON file (overrides --poll-calendar)",
    )
    parser.add_argument(
        "--poll-calendar",
        action="store_true",
        help="Fetch today's events from all Google Calendars (all 3 accounts)",
    )
    parser.add_argument(
        "--inbox-input",
        help="Personal inbox context as JSON file (overrides --poll-inbox)",
    )
    parser.add_argument(
        "--poll-inbox",
        action="store_true",
        help="Fetch agent and personal inboxes from Gmail API",
    )
    parser.add_argument(
        "--no-notion",
        action="store_true",
        help="Skip Notion task creation",
    )
    parser.add_argument(
        "--print-payload",
        action="store_true",
        help="Print normalized payload before running",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build payload but don't run routine",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    repo_root = _repo_root()
    src_path = repo_root / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))

    from agentic_os.cli import print_json
    from agentic_os.config import default_paths
    from agentic_os.gmail_task_creator import create_tasks_from_inbox
    from agentic_os.openclaw_bridge import normalize_openclaw_daily_routine_payload
    from agentic_os.service import AgenticOSService

    parser = _build_parser()
    args = parser.parse_args(argv)

    # Load context — file inputs override live polling
    calendar = None
    if args.calendar_input:
        calendar = json.loads(Path(args.calendar_input).read_text(encoding="utf-8"))
    elif getattr(args, "poll_calendar", False):
        from agentic_os.calendar_poller import poll_calendar
        calendar = poll_calendar()

    personal_inbox = None
    agent_inbox = None
    if args.inbox_input:
        personal_inbox = json.loads(Path(args.inbox_input).read_text(encoding="utf-8"))
    elif getattr(args, "poll_inbox", False):
        from agentic_os.gmail_poller import poll_all_inboxes
        polled = poll_all_inboxes()
        personal_inbox = polled.get("personal_inbox")
        agent_inbox = polled.get("agent_inbox")

    # Build raw payload
    payload = _build_daily_routine_payload(
        date=args.date,
        timezone_name=args.timezone,
        recipient=args.recipient,
        delivery_time=args.delivery_time,
        calendar=calendar,
        personal_inbox=personal_inbox,
        agent_inbox=agent_inbox,
    )

    # Normalize through bridge
    normalized_payload = normalize_openclaw_daily_routine_payload(payload)

    if args.print_payload or args.dry_run:
        print_json({"normalized_payload": normalized_payload})

    if args.dry_run:
        return 0

    # Run the routine
    paths = default_paths()
    service = AgenticOSService(paths)
    service.initialize()
    result = service.run_daily_routine(
        payload=normalized_payload,
        create_notion_tasks=not args.no_notion,
    )

    personal_inbox_summary = normalized_payload.get("personal_inbox", {})
    agent_inbox_summary = normalized_payload.get("agent_inbox", {})
    new_tasks = create_tasks_from_inbox(personal_inbox_summary, "personal", paths=paths)
    new_tasks += create_tasks_from_inbox(agent_inbox_summary, "agent", paths=paths)
    print(f"Created {len(new_tasks)} tasks from inbox", file=sys.stderr)
    result["created_inbox_tasks"] = new_tasks
    result["created_inbox_task_count"] = len(new_tasks)

    print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
