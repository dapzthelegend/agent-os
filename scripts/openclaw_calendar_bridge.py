#!/usr/bin/env python3
"""
OpenClaw calendar bridge — calendar write operations for the agent inbox.

This is the script the agent calls when it needs to create, update, block,
delete events, or add reminders on the franchieinc@gmail.com calendar.

All writes go to the agent calendar only (franchieinc). Personal calendars
(dapz, sola) are read-only and cannot be mutated here.

Usage
-----
The agent passes a JSON payload describing the operation:

  python3 scripts/openclaw_calendar_bridge.py --input-json '<JSON>'
  python3 scripts/openclaw_calendar_bridge.py --input-file /tmp/cal_op.json

Payload schema (all fields except "action" are operation-specific):

  Create event:
    {"action": "create_event",
     "title": "...", "start": "2026-03-25T10:00:00", "end": "2026-03-25T11:00:00",
     "description": "...", "location": "...",
     "attendees": ["email@example.com"],
     "reminders_minutes": [10, 30],
     "timezone": "Europe/London"}

  Block time (no attendees, focused slot):
    {"action": "block_time",
     "title": "Deep work", "start": "2026-03-25T09:00:00", "end": "2026-03-25T12:00:00",
     "description": "...", "timezone": "Europe/London"}

  Update event:
    {"action": "update_event",
     "event_id": "...",
     "title": "...", "start": "...", "end": "...",
     "description": "...", "location": "...",
     "reminders_minutes": [15],
     "timezone": "Europe/London"}

  Delete event:
    {"action": "delete_event", "event_id": "..."}

  Add reminder:
    {"action": "add_reminder", "event_id": "...", "minutes": 30}

  List today's events (read, all 3 accounts):
    {"action": "list_events"}

Output
------
Always prints a single JSON object to stdout:
  {"ok": true, "action": "...", "result": {...}}   on success
  {"ok": false, "action": "...", "error": "..."}   on failure

Exit code 0 on success, 2 on failure.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_payload(args) -> dict:
    if args.input_json and args.input_file:
        raise SystemExit("provide either --input-file or --input-json, not both")
    if args.input_json:
        return json.loads(args.input_json)
    if args.input_file:
        return json.loads(Path(args.input_file).read_text(encoding="utf-8"))
    raise SystemExit("provide --input-file or --input-json")


def _build_parser():
    import argparse
    parser = argparse.ArgumentParser(
        description="OpenClaw calendar bridge — execute a calendar action from a JSON payload"
    )
    parser.add_argument("--input-json", help="Inline JSON payload string")
    parser.add_argument("--input-file", help="Path to JSON payload file")
    return parser


def main(argv=None) -> int:
    repo_root = _repo_root()
    src_path = repo_root / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))

    parser = _build_parser()
    args = parser.parse_args(argv)
    payload = _load_payload(args)

    action = payload.get("action")
    if not action:
        print(json.dumps({"ok": False, "error": "payload missing 'action' field"}))
        return 2

    try:
        result = _dispatch(action, payload)
        print(json.dumps({"ok": True, "action": action, "result": result}))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "action": action, "error": str(exc)}))
        return 2


def _dispatch(action: str, payload: dict) -> dict:
    if action == "list_events":
        from agentic_os.calendar_poller import poll_calendar
        return poll_calendar()

    if action == "create_event":
        from agentic_os.calendar_writer import create_event
        return create_event(
            title=payload["title"],
            start=payload["start"],
            end=payload["end"],
            description=payload.get("description"),
            location=payload.get("location"),
            attendees=payload.get("attendees"),
            reminders_minutes=payload.get("reminders_minutes"),
            timezone=payload.get("timezone", "Europe/London"),
        )

    if action == "block_time":
        from agentic_os.calendar_writer import block_time
        return block_time(
            title=payload["title"],
            start=payload["start"],
            end=payload["end"],
            description=payload.get("description"),
            timezone=payload.get("timezone", "Europe/London"),
        )

    if action == "update_event":
        from agentic_os.calendar_writer import update_event
        return update_event(
            payload["event_id"],
            title=payload.get("title"),
            start=payload.get("start"),
            end=payload.get("end"),
            description=payload.get("description"),
            location=payload.get("location"),
            reminders_minutes=payload.get("reminders_minutes"),
            timezone=payload.get("timezone", "Europe/London"),
        )

    if action == "delete_event":
        from agentic_os.calendar_writer import delete_event
        ok = delete_event(payload["event_id"])
        return {"deleted": ok, "event_id": payload["event_id"]}

    if action == "add_reminder":
        from agentic_os.calendar_writer import add_reminder
        return add_reminder(
            payload["event_id"],
            minutes=payload["minutes"],
        )

    raise ValueError(f"Unknown action: {action!r}. Valid actions: "
                     "list_events, create_event, block_time, update_event, "
                     "delete_event, add_reminder")


if __name__ == "__main__":
    raise SystemExit(main())
