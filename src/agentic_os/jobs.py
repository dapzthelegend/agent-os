"""
Job registry — the single place that defines what runs and when.

Each job is a `ScheduledJob` subscribed to the `BackgroundScheduler`.
The scheduler itself knows nothing about these; this module owns all
timing and action knowledge.

To add a new job: define a `_make_<name>_job()` factory and call it
inside `register_all_jobs()`.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from .scheduler import BackgroundScheduler, ScheduledJob

if TYPE_CHECKING:
    from .config import AppConfig, Paths

DISCORD_APPROVAL_REMINDER_PUSH_ENABLED_ENV = "DISCORD_APPROVAL_REMINDER_PUSH_ENABLED"

# ---------------------------------------------------------------------------
# Helpers — next-run-at factories
# ---------------------------------------------------------------------------

def _every(seconds: int):
    """Returns a `next_run_at` callable for a fixed interval."""
    def _next() -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return _next


def _daily_at_utc(hour: int, minute: int):
    """Returns a `next_run_at` callable for daily HH:MM UTC."""
    def _next() -> datetime:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target
    return _next



# ---------------------------------------------------------------------------
# Job factories
# ---------------------------------------------------------------------------

def _make_reconcile_job(paths: "Paths", config: "AppConfig") -> ScheduledJob:
    def _run():
        from .paperclip_reconciler import PaperclipReconciler
        return PaperclipReconciler(paths, config).run_once()

    return ScheduledJob(
        name="paperclip-reconcile",
        func=_run,
        next_run_at=_every(120),
        run_immediately=True,
    )


def _make_approval_reminder_job(paths: "Paths", config: "AppConfig") -> ScheduledJob:
    def _run():
        from .service import AgenticOSService
        return AgenticOSService(paths, config).send_approval_reminders()

    return ScheduledJob(
        name="approval-reminder",
        func=_run,
        next_run_at=_every(3600),
        run_immediately=False,
    )


def _make_health_check_job(paths: "Paths", config: "AppConfig") -> ScheduledJob:
    def _run():
        from .service import AgenticOSService
        from .health import get_system_health
        service = AgenticOSService(paths, config)
        health = get_system_health(service)
        if health.get("status") != "ok":
            _alert_health(health)
        return {"status": health.get("status")}

    return ScheduledJob(
        name="health-check",
        func=_run,
        next_run_at=_every(3600),
        run_immediately=False,
    )


def _make_backup_job(paths: "Paths", config: "AppConfig") -> ScheduledJob:
    def _run():
        from .backup import main as run_backup
        run_backup()
        return {"status": "ok"}

    return ScheduledJob(
        name="workspace-backup",
        func=_run,
        next_run_at=_daily_at_utc(hour=2, minute=0),
        run_immediately=False,
    )


def _make_discord_approval_poll_job(paths: "Paths", config: "AppConfig") -> ScheduledJob:
    def _run():
        from .discord_approval_poller import poll_discord_approvals
        from .service import AgenticOSService
        service = AgenticOSService(paths, config)
        return poll_discord_approvals(paths, service)

    return ScheduledJob(
        name="discord-approval-poll",
        func=_run,
        next_run_at=_every(60),
        run_immediately=True,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def register_all_jobs(scheduler: BackgroundScheduler, paths: "Paths", config: "AppConfig") -> None:
    """Register all agentic-os background jobs with the given scheduler."""
    scheduler.register(_make_reconcile_job(paths, config))
    if os.environ.get(DISCORD_APPROVAL_REMINDER_PUSH_ENABLED_ENV, "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        scheduler.register(_make_approval_reminder_job(paths, config))
    scheduler.register(_make_health_check_job(paths, config))
    scheduler.register(_make_backup_job(paths, config))
    scheduler.register(_make_discord_approval_poll_job(paths, config))
    # Daily recap is owned by the Paperclip routine:
    # http://127.0.0.1:3100/FRA/routines/8d3baf2d-4615-4461-aa11-f2886f41237d


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _alert_health(health: dict) -> None:
    import logging
    log = logging.getLogger(__name__)
    try:
        from .notification_router import _send_with_fallback
        status = health.get("status", "unknown")
        problems = []
        if not health.get("db", {}).get("reachable"):
            problems.append("DB unreachable")
        for job in health.get("cron", {}).get("jobs", []):
            if job.get("status") == "error":
                problems.append(f"job {job['id']} erroring")
        detail = "; ".join(problems) or "see /health"
        _send_with_fallback(
            f"agentic-os health check: **{status}** — {detail}",
            subject=f"agentic-os health check: {status}",
        )
    except Exception as exc:
        log.warning("health alert send failed: %s", exc)
