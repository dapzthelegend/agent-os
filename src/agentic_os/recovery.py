"""
Task timeout and recovery — Phase 7.1.

This module is the engine behind:
  • The stall-check cron job that flags in_progress tasks silent for >2h.
  • The `cli retry <task_id>` operator command.
  • Discord DM alerts when a task is flagged as stalled.

Usage (cron job):
    from agentic_os.recovery import scan_and_flag_stalled_tasks
    from agentic_os.service import AgenticOSService
    result = scan_and_flag_stalled_tasks(service)

Usage (operator CLI):
    python3 -m agentic_os.cli retry <task_id>
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .service import AgenticOSService


# Tasks that have been in_progress for longer than this (hours) without a status
# update are considered stalled.  The cron job uses the same default.
DEFAULT_STALL_THRESHOLD_HOURS: float = 2.0

# Statuses that can become stalled.  Only actively running tasks apply.
_ACTIVE_STATUSES = frozenset({"in_progress", "awaiting_input"})

# Terminal statuses — stalled tasks reset to in_progress on retry; these are
# the terminal statuses where a retry is meaningless.
_TERMINAL_STATUSES = frozenset({"completed", "executed", "cancelled"})


@dataclass
class StallReport:
    task_id: str
    status: str
    hours_since_update: float
    notified: bool


def find_stalled_tasks(
    service: "AgenticOSService",
    threshold_hours: float = DEFAULT_STALL_THRESHOLD_HOURS,
) -> list[dict]:
    """
    Return tasks that have been in an active status without an update for
    longer than threshold_hours (or a per-domain threshold from config).

    Domain-specific thresholds are read from service.config.stall_thresholds.
    Keys: domain name (e.g. "content", "personal") or "default" as fallback.
    The CLI --threshold-hours flag is used only when no config value applies.

    Returns a list of dicts with keys:
        task_id, status, updated_at, hours_since_update, domain, threshold_used
    """
    now = datetime.now(timezone.utc)
    thresholds: dict[str, float] = getattr(service.config, "stall_thresholds", {}) or {}
    candidates = service.db.query_tasks(limit=500)
    stalled = []
    for task in candidates:
        if task.status not in _ACTIVE_STATUSES:
            continue
        # Per-domain threshold: domain key → "default" key → CLI arg
        domain_threshold = (
            thresholds.get(task.domain)
            or thresholds.get("default")
            or threshold_hours
        )
        hours = _hours_elapsed(task.updated_at, now)
        if hours >= domain_threshold:
            stalled.append(
                {
                    "task_id": task.id,
                    "status": task.status,
                    "updated_at": task.updated_at,
                    "hours_since_update": round(hours, 2),
                    "domain": task.domain,
                    "threshold_used": domain_threshold,
                    "user_request": task.user_request[:120],
                }
            )
    stalled.sort(key=lambda d: d["hours_since_update"], reverse=True)
    return stalled


def flag_stalled_task(
    service: "AgenticOSService",
    task_id: str,
    *,
    hours_overdue: Optional[float] = None,
) -> StallReport:
    """
    Mark a single task as stalled, append audit event, and fire Discord DM.

    Safe to call on a task that is already stalled — the status write is
    idempotent and no second notification is sent.
    """
    task = service.db.get_task(task_id)
    now = datetime.now(timezone.utc)

    if hours_overdue is None:
        hours_overdue = _hours_elapsed(task.updated_at, now)

    already_stalled = task.status == "stalled"

    if not already_stalled:
        task = service.db.update_task(task_id, status="stalled")
        service._append_event(
            task_id=task_id,
            event_type="task_stalled",
            payload={
                "previous_status": task.status,
                "hours_since_update": round(hours_overdue, 2),
            },
        )

    # Always attempt Discord notification for new stalls
    notified = False
    if not already_stalled:
        try:
            from .notifier import notify_overdue_task
            notified = notify_overdue_task(task, hours_overdue)
        except Exception:
            pass

    return StallReport(
        task_id=task_id,
        status="stalled",
        hours_since_update=round(hours_overdue, 2),
        notified=notified,
    )


def scan_and_flag_stalled_tasks(
    service: "AgenticOSService",
    threshold_hours: float = DEFAULT_STALL_THRESHOLD_HOURS,
) -> dict:
    """
    Find all stalled tasks, flag them, and send Discord alerts.

    Designed to be called by the stall-check cron job.

    Returns:
        {
            "checked": <int>,
            "flagged": <int>,
            "already_stalled": <int>,
            "notified": <int>,
            "tasks": [StallReport, ...]
        }
    """
    candidates = find_stalled_tasks(service, threshold_hours=threshold_hours)
    reports: list[StallReport] = []

    for item in candidates:
        report = flag_stalled_task(
            service,
            item["task_id"],
            hours_overdue=item["hours_since_update"],
        )
        reports.append(report)

    flagged = sum(1 for r in reports if r.hours_since_update >= 0)
    notified = sum(1 for r in reports if r.notified)

    return {
        "checked": len(candidates),
        "flagged": len(reports),
        "notified": notified,
        "tasks": [
            {
                "task_id": r.task_id,
                "status": r.status,
                "hours_since_update": r.hours_since_update,
                "notified": r.notified,
            }
            for r in reports
        ],
    }


def retry_stalled_task(
    service: "AgenticOSService",
    task_id: str,
    *,
    feedback: str = "operator retry",
) -> dict:
    """
    Reset a stalled (or failed) task back to in_progress.

    Calls service.reset_task_for_retry() which enforces the max-2-retry limit.
    Returns {task_id, new_status, retry_count, message}.
    """
    task = service.db.get_task(task_id)

    if task.status in _TERMINAL_STATUSES:
        return {
            "task_id": task_id,
            "new_status": task.status,
            "retry_count": task.retry_count or 0,
            "message": f"task {task_id} is {task.status} — cannot retry terminal tasks",
        }

    # If task was stalled, first emit a stall-cleared event before reset
    if task.status == "stalled":
        service._append_event(
            task_id=task_id,
            event_type="task_stall_cleared",
            payload={"cleared_by": "operator_retry", "feedback": feedback},
        )

    updated = service.reset_task_for_retry(task_id, feedback=feedback)
    return {
        "task_id": task_id,
        "new_status": updated.status,
        "retry_count": updated.retry_count or 0,
        "message": (
            f"task reset to {updated.status} (retry {updated.retry_count})"
            if updated.status == "in_progress"
            else f"max retries exceeded — task {task_id} marked {updated.status}"
        ),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _hours_elapsed(iso_timestamp: str, now: datetime) -> float:
    """Return hours elapsed since iso_timestamp (UTC)."""
    try:
        ts = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() / 3600
    except Exception:
        return 0.0
