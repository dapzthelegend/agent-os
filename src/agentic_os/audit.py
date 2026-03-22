from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


EVENT_TYPES = (
    "task_created",
    "task_classified",
    "policy_evaluated",
    "operation_rejected",
    "adapter_called",
    "adapter_result",
    "adapter_failed",
    "tool_called",
    "tool_result",
    "draft_created",
    "draft_generated",
    "artifact_updated",
    "daily_routine_recap_created",
    "daily_routine_email_prepared",
    "daily_routine_followup_created",
    "daily_routine_followups_created",
    "notion_sync_imported",
    "summary_recorded",
    "approval_requested",
    "approval_granted",
    "approval_denied",
    "approval_cancelled",
    "action_executed",
    "action_execution_requested",
    "action_execution_recorded",
    "action_execution_rejected",
    "task_completed",
    "task_failed",
    "task_cancelled",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def ensure(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def append(
        self,
        *,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
        event_id: int,
    ) -> dict[str, Any]:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unsupported event_type: {event_type}")
        event = {
            "id": event_id,
            "task_id": task_id,
            "event_type": event_type,
            "payload": payload,
            "created_at": utc_now(),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
        return event

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def read_for_task(self, task_id: str) -> Iterable[dict[str, Any]]:
        for event in self.read_all():
            if event["task_id"] == task_id:
                yield event
