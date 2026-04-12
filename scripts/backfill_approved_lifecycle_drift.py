#!/usr/bin/env python3
"""
Backfill lifecycle drift where approval updates regressed completed tasks.

A task is considered drifted when:
- current status is "approved"
- it has a "task_completed" audit event
- it has an "approval_granted" audit event that happened after completion

Target status:
- "executed" when an artifact_ref exists
- otherwise "completed"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from agentic_os.config import default_paths, load_app_config
from agentic_os.service import AgenticOSService


def _event_markers(service: AgenticOSService, task_id: str) -> tuple[int | None, int | None]:
    completed_event_id: int | None = None
    latest_approval_event_id: int | None = None
    for event in service.db.list_audit_events(task_id):
        event_id = int(event["id"])
        if event["event_type"] == "task_completed":
            if completed_event_id is None or event_id < completed_event_id:
                completed_event_id = event_id
        elif event["event_type"] == "approval_granted":
            if latest_approval_event_id is None or event_id > latest_approval_event_id:
                latest_approval_event_id = event_id
    return completed_event_id, latest_approval_event_id


def _find_drifted_tasks(service: AgenticOSService) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for task in service.db.list_tasks(limit=10000):
        if task.status != "approved":
            continue
        completed_event_id, approval_event_id = _event_markers(service, task.id)
        if completed_event_id is None or approval_event_id is None:
            continue
        if approval_event_id <= completed_event_id:
            continue
        target_status = "executed" if task.artifact_ref else "completed"
        candidates.append(
            {
                "task_id": task.id,
                "paperclip_issue_id": task.paperclip_issue_id,
                "from_status": task.status,
                "to_status": target_status,
                "completed_event_id": completed_event_id,
                "approval_event_id": approval_event_id,
                "artifact_ref": task.artifact_ref,
            }
        )
    return candidates


def _sync_paperclip_status(service: AgenticOSService, task_id: str) -> None:
    cp = service._cp
    if cp is None:
        return
    task = service.db.get_task(task_id)
    if not task.paperclip_issue_id:
        return
    try:
        executor_key = cp.resolve_executor_key(task)
        cp.update_issue_status(task.paperclip_issue_id, task.status, assignee_key=executor_key)
    except Exception as exc:  # noqa: BLE001
        service._append_event(
            task_id=task.id,
            event_type="paperclip_sync_failed",
            payload={"error": str(exc), "op": "backfill_lifecycle_drift"},
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill tasks regressed to approved after execution completion."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply updates. Without this flag, runs in dry-run mode.",
    )
    args = parser.parse_args()

    paths = default_paths()
    config = load_app_config(paths)
    service = AgenticOSService(paths, config)
    service.initialize()

    candidates = _find_drifted_tasks(service)
    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry_run",
                "candidate_count": len(candidates),
            }
        )
    )
    for item in candidates:
        print(json.dumps(item))

    if not args.apply:
        return 0

    updated = 0
    for item in candidates:
        task_id = str(item["task_id"])
        updated_task = service.db.update_task(
            task_id,
            status=str(item["to_status"]),
            approval_state="approved",
        )
        service._append_event(
            task_id=task_id,
            event_type="summary_recorded",
            payload={
                "from_status": item["from_status"],
                "to_status": item["to_status"],
                "reason": "approval_granted_after_task_completed",
                "completed_event_id": item["completed_event_id"],
                "approval_event_id": item["approval_event_id"],
            },
        )
        _sync_paperclip_status(service, task_id)
        updated += 1
        print(
            json.dumps(
                {
                    "updated_task_id": updated_task.id,
                    "status": updated_task.status,
                    "paperclip_issue_id": updated_task.paperclip_issue_id,
                }
            )
        )

    print(json.dumps({"updated_count": updated}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
