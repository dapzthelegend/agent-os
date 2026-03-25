from __future__ import annotations

import gzip
import json
import shutil
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
    "notion_update_failed",
    "summary_recorded",
    "approval_requested",
    "approval_granted",
    "approval_denied",
    "approval_cancelled",
    "action_executed",
    "action_execution_requested",
    "action_execution_recorded",
    "action_execution_rejected",
    "execution_callback_received",
    "task_completed",
    "task_failed",
    "task_cancelled",
    "task_retry_reset",
    "task_stalled",
    "task_stall_cleared",
    # Execution loop (Phase 1/2)
    "task_picked_up",
    "task_dispatched",
    "task_requeued",
    "spawn_failed",
    # Paperclip projection (Phase 1)
    "paperclip_issue_created",
    "paperclip_projection_failed",
    "paperclip_sync_failed",
    # Plan gate (Phase 2)
    "task_mode_set",
    "plan_submitted",
    "plan_awaiting_review",
    "plan_approved",
    "plan_rejected",
    # Paperclip reconciler (Phase 4)
    "reconciler_ran",
    "reconciler_action_taken",
    "reconciler_comment_ignored",
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

    def rotate(self, *, size_threshold_bytes: int = 10 * 1024 * 1024, gzip_after_days: int = 7) -> dict[str, Any]:
        """
        Rotate the live audit log when it exceeds size_threshold_bytes.

        Rotation renames the current log to audit_log.YYYY-MM-DD.jsonl and starts
        a fresh file.  Any rotated archives older than gzip_after_days that are not
        yet compressed are gzipped in place.

        Returns a summary dict: {rotated, archived, size_bytes}.
        """
        rotated = False
        archived: list[str] = []

        if self.path.exists() and self.path.stat().st_size >= size_threshold_bytes:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            dest = self.path.with_name(f"audit_log.{stamp}.jsonl")
            # If a file with that name already exists (two rotations in one day),
            # append a counter suffix.
            counter = 1
            while dest.exists():
                dest = self.path.with_name(f"audit_log.{stamp}.{counter}.jsonl")
                counter += 1
            shutil.move(str(self.path), str(dest))
            self.ensure()
            rotated = True

        # Gzip any uncompressed archive files older than gzip_after_days
        now = datetime.now(timezone.utc)
        for candidate in self.path.parent.glob("audit_log.*.jsonl"):
            if candidate == self.path:
                continue
            age_days = (now.timestamp() - candidate.stat().st_mtime) / 86400
            if age_days >= gzip_after_days:
                gz_path = candidate.with_suffix(".jsonl.gz")
                with candidate.open("rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
                candidate.unlink()
                archived.append(gz_path.name)

        return {
            "rotated": rotated,
            "archived": archived,
            "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }
