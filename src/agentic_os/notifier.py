"""
Send completion/failure notifications via Discord (with stderr fallback).

Thin shim over notification_router — keeps the existing call-site API stable
while delegating to the smart routing layer.
"""
from __future__ import annotations

from typing import Optional

from .models import ApprovalRecord, TaskRecord
from .notification_router import (
    NotificationResult,
    route_approval_reminder,
    route_approval_requested,
    route_overdue_task,
    route_task_completed,
    route_task_failed,
)

# Re-export so existing imports keep working
__all__ = [
    "notify_task_completed",
    "notify_task_failed",
    "notify_approval_requested",
    "notify_approval_reminder",
    "notify_overdue_task",
]


def notify_task_completed(
    task: TaskRecord,
    *,
    result_link: Optional[str] = None,
    word_count: Optional[int] = None,
) -> bool:
    """
    Send Discord notification for task completion.

    Returns True if delivered to Discord, False if fell back to stderr.
    """
    result = route_task_completed(task, result_link=result_link, word_count=word_count)
    return result.channel == "discord" and result.success


def notify_task_failed(task: TaskRecord) -> bool:
    """
    Send Discord notification for task failure.

    Returns True if delivered to Discord, False if fell back to stderr.
    """
    result = route_task_failed(task)
    return result.channel == "discord" and result.success


def notify_approval_requested(
    task: TaskRecord,
    approval: ApprovalRecord,
) -> bool:
    """
    Send Discord DM for a new approval request with approve/deny instructions.

    Returns True if delivered to Discord, False if fell back to stderr.
    """
    result = route_approval_requested(task, approval)
    return result.channel == "discord" and result.success


def notify_approval_reminder(
    task: TaskRecord,
    approval: ApprovalRecord,
    hours_pending: float,
) -> bool:
    """
    Send a reminder notification if an approval has been pending >1h.

    Returns True if delivered to Discord, False if fell back to stderr.
    """
    result = route_approval_reminder(task, approval, hours_pending)
    return result.channel == "discord" and result.success


def notify_overdue_task(task: TaskRecord, hours_overdue: float) -> bool:
    """
    Send an overdue-task alert when a task has had no update for >48h.

    Returns True if delivered to Discord, False if fell back to stderr.
    """
    result = route_overdue_task(task, hours_overdue)
    return result.channel == "discord" and result.success
