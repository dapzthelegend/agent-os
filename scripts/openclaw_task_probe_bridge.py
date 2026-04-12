#!/usr/bin/env python3
"""
OpenClaw task probe bridge.

Returns a compact, machine-readable task update payload so OpenClaw runtimes
can answer "what is the latest status?" probes consistently.

Input payload (JSON object) supports:
  {"action":"probe_task","task_id":"<task_id>","events_limit":8}
  {"action":"probe_task","paperclip_issue_id":"<issue_id>"}
  {"action":"probe_task","operation_key":"<operation_key>"}

Output:
  {"ok": true, "action":"probe_task", "result": {...}}
  {"ok": false, "action":"probe_task", "error":"..."}
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OpenClaw bridge: probe a backend task and return latest status updates",
    )
    parser.add_argument("--input-json", help="Inline JSON payload")
    parser.add_argument("--input-file", help="Path to JSON payload file")
    return parser


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.input_json and args.input_file:
        raise SystemExit("provide either --input-file or --input-json, not both")
    if args.input_json:
        payload = json.loads(args.input_json)
    elif args.input_file:
        payload = json.loads(Path(args.input_file).read_text(encoding="utf-8"))
    else:
        raise SystemExit("provide --input-file or --input-json")
    if not isinstance(payload, dict):
        raise SystemExit("input payload must decode to a JSON object")
    return payload


def _recent_events(events: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        limit = 1
    tail = events[-limit:]
    out: list[dict[str, Any]] = []
    for event in tail:
        out.append(
            {
                "created_at": event.get("created_at"),
                "event_type": event.get("event_type"),
                "payload": event.get("payload"),
            }
        )
    return out


def _resolve_task_id(service: Any, payload: dict[str, Any]) -> str:
    task_id = str(payload.get("task_id") or "").strip()
    if task_id:
        return task_id

    operation_key = str(payload.get("operation_key") or "").strip()
    if operation_key:
        matches = service.db.list_tasks_by_operation_key(operation_key)
        if not matches:
            raise ValueError(f"no task found for operation_key: {operation_key}")
        return matches[0].id

    paperclip_issue_id = str(payload.get("paperclip_issue_id") or "").strip()
    if paperclip_issue_id:
        result = service.ensure_task_for_paperclip_issue(paperclip_issue_id)
        resolved_task_id = str(result.get("task_id") or "").strip()
        if not resolved_task_id:
            raise ValueError(f"unable to resolve task for paperclip_issue_id: {paperclip_issue_id}")
        return resolved_task_id

    raise ValueError("probe_task requires one of: task_id, operation_key, paperclip_issue_id")


def _build_probe_result(service: Any, task_id: str, events_limit: int) -> dict[str, Any]:
    detail = service.get_task_detail(task_id)
    task = detail["task"]
    events = detail.get("audit_events", [])
    latest_event = events[-1] if events else None

    return {
        "probed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "task_id": task["id"],
        "paperclip_issue_id": task.get("paperclip_issue_id"),
        "status": task["status"],
        "approval_state": task.get("approval_state"),
        "task_mode": task.get("task_mode"),
        "domain": task.get("domain"),
        "intent_type": task.get("intent_type"),
        "risk_level": task.get("risk_level"),
        "claimed_by": task.get("claimed_by"),
        "dispatch_session_key": task.get("dispatch_session_key"),
        "result_summary": task.get("result_summary"),
        "last_event": {
            "created_at": latest_event.get("created_at"),
            "event_type": latest_event.get("event_type"),
        }
        if latest_event
        else None,
        "recent_events": _recent_events(events, events_limit),
    }


def _dispatch(service: Any, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action != "probe_task":
        raise ValueError("Unknown action. Valid actions: probe_task")
    events_limit = int(payload.get("events_limit", 8))
    task_id = _resolve_task_id(service, payload)
    return _build_probe_result(service, task_id, events_limit)


def main(argv: Optional[list[str]] = None) -> int:
    repo_root = _repo_root()
    src_path = repo_root / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))

    from agentic_os.config import default_paths
    from agentic_os.service import AgenticOSService

    parser = _build_parser()
    args = parser.parse_args(argv)
    payload = _load_payload(args)
    action = str(payload.get("action") or "probe_task").strip() or "probe_task"

    try:
        service = AgenticOSService(default_paths())
        service.initialize()
        result = _dispatch(service, action, payload)
        print(json.dumps({"ok": True, "action": action, "result": result}))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "action": action, "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
