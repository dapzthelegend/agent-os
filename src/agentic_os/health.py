"""
System health checks — Phase 7.3 & 7.5.

Provides:
  • get_system_health(service)  — full health snapshot for GET /health
  • validate_startup_config(paths, config)  — called by service.initialize()

Health snapshot schema
----------------------
{
    "status": "ok" | "degraded" | "error",
    "checked_at": "<ISO8601>",
    "db": {
        "reachable": true,
        "row_counts": {
            "tasks": <int>,
            "approvals": <int>,
            "executions": <int>,
            "artifacts": <int>,
            "audit_events": <int>
        }
    },
    "audit_log": {
        "exists": true,
        "size_bytes": <int>,
        "last_event_at": "<ISO8601>" | null
    },
    "artifacts_dir": {
        "writable": true,
        "path": "<str>"
    },
    "config": {
        "paperclip_configured": true,
        "issues": []
    },
    "cron": {
        "jobs": [
            {
                "id": "<str>",
                "last_run_at": "<ISO8601>" | null,
                "consecutive_errors": <int>,
                "status": "ok" | "warning" | "error"
            }
        ]
    }
}
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .config import AppConfig, Paths
    from .service import AgenticOSService


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_system_health(service: "AgenticOSService") -> dict[str, Any]:
    """
    Return the full health snapshot.  Never raises — any error is captured
    inside the relevant section with status → "error".
    """
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    db_section = _check_db(service)
    audit_section = _check_audit_log(service.paths.audit_log_path)
    artifacts_section = _check_artifacts_dir(service.paths.artifacts_dir)
    config_section = _check_config(service.config)
    cron_section = _check_cron(service.paths.root)

    issues = (
        (not db_section.get("reachable", False))
        or (not artifacts_section.get("writable", False))
        or bool(config_section.get("issues"))
        or any(j.get("status") == "error" for j in cron_section.get("jobs", []))
    )
    degraded = any(j.get("status") == "warning" for j in cron_section.get("jobs", []))

    overall = "ok"
    if issues:
        overall = "error"
    elif degraded:
        overall = "degraded"

    return {
        "status": overall,
        "checked_at": checked_at,
        "db": db_section,
        "audit_log": audit_section,
        "artifacts_dir": artifacts_section,
        "config": config_section,
        "cron": cron_section,
    }


def get_paperclip_health(service: "AgenticOSService") -> dict[str, Any]:
    """
    Return Paperclip connectivity and reconciler status.

    Schema:
        configured: bool
        base_url: str | None
        company_id: str | None
        reachable: bool | None   (None = not configured)
        last_reconcile_at: ISO8601 str | None
        tasks_with_issue: int
        tasks_without_issue: int
    """
    config = service.config
    if config.paperclip is None:
        return {
            "configured": False,
            "base_url": None,
            "company_id": None,
            "reachable": None,
            "last_reconcile_at": None,
            "tasks_with_issue": 0,
            "tasks_without_issue": 0,
        }

    reachable = _check_paperclip_reachable(config.paperclip.base_url)
    last_reconcile_at = _get_last_reconcile_at(service)
    task_counts = _get_paperclip_task_counts(service)

    return {
        "configured": True,
        "base_url": config.paperclip.base_url,
        "company_id": config.paperclip.company_id,
        "reachable": reachable,
        "last_reconcile_at": last_reconcile_at,
        "tasks_with_issue": task_counts["with_issue"],
        "tasks_without_issue": task_counts["without_issue"],
    }


def get_paperclip_diagnostics(
    service: "AgenticOSService",
    *,
    task_id: Optional[str] = None,
    issue_id: Optional[str] = None,
    activity_lookback_seconds: int = 86400,
) -> dict[str, Any]:
    """Return a focused backend+Paperclip diagnostic snapshot for one task/issue."""
    if not task_id and not issue_id:
        raise ValueError("provide task_id or issue_id")

    out: dict[str, Any] = {
        "configured": service.config.paperclip is not None,
        "requested": {
            "task_id": task_id,
            "issue_id": issue_id,
            "activity_lookback_seconds": activity_lookback_seconds,
        },
    }

    task = None
    task_lookup_error: Optional[str] = None
    if task_id:
        try:
            task = service.db.get_task(task_id)
        except KeyError:
            task_lookup_error = f"unknown task_id: {task_id}"

    if task is None and issue_id:
        task = service.db.get_task_by_paperclip_issue_id(issue_id)

    if task is not None:
        out["task"] = asdict(task)
        detail = service.get_task_detail(task.id)
        out["task_audit_events"] = detail.get("audit_events", [])[-30:]
        out["task_artifacts"] = detail.get("artifacts", [])
    elif task_lookup_error:
        out["task_lookup_error"] = task_lookup_error

    resolved_issue_id = issue_id or (task.paperclip_issue_id if task is not None else None)
    out["resolved_issue_id"] = resolved_issue_id
    if not resolved_issue_id:
        out["note"] = "No Paperclip issue linked to the resolved task."
        return out

    if service.config.paperclip is None:
        out["paperclip_error"] = "paperclip not configured"
        return out

    cp = service._cp
    if cp is None:
        out["paperclip_error"] = "paperclip control plane unavailable"
        return out

    issue = cp.get_issue(resolved_issue_id)
    out["paperclip_issue"] = None if issue is None else asdict(issue)

    comments = cp.list_comments(resolved_issue_id)
    out["paperclip_comments"] = [
        {"id": comment.id, "issue_id": comment.issue_id, "body": comment.body, "body_length": len(comment.body)}
        for comment in comments
    ]

    activity = cp.poll_activity(resolved_issue_id, lookback_seconds=activity_lookback_seconds)
    out["paperclip_activity"] = [
        {
            "id": event.id,
            "event_type": event.event_type,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "run_id": event.run_id,
            "created_at": event.created_at,
            "payload": event.payload or {},
            "details": event.details or {},
        }
        for event in activity
    ]

    plan_doc = cp.get_document(resolved_issue_id, "plan")
    out["paperclip_plan_document"] = (
        None
        if plan_doc is None
        else {
            "id": plan_doc.id,
            "issue_id": plan_doc.issue_id,
            "title": plan_doc.title,
            "content": plan_doc.content,
            "content_length": len(plan_doc.content),
        }
    )
    out["checks"] = {
        "has_task_link": task is not None,
        "has_issue_projection": issue is not None,
        "comments_visible": len(comments) > 0,
        "plan_document_visible": plan_doc is not None and bool(plan_doc.content),
    }
    return out


def get_watchdog_status(service: "AgenticOSService") -> dict[str, Any]:
    """Return a lightweight liveness payload for the external supervisor."""
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    db_section = _check_db(service)
    scheduler_state = "unknown"
    try:
        scheduler = getattr(service, "scheduler", None)
        if scheduler is not None:
            scheduler_state = "running"
    except Exception:
        scheduler_state = "error"

    healthy = bool(db_section.get("reachable"))
    return {
        "status": "ok" if healthy else "error",
        "checked_at": checked_at,
        "db_reachable": bool(db_section.get("reachable")),
        "scheduler": scheduler_state,
    }


def validate_startup_config(paths: "Paths", config: "AppConfig") -> list[str]:
    """
    Validate essential config at startup.  Returns a list of issue strings
    (empty = all good).  Does NOT raise — callers decide what to do.
    """
    issues: list[str] = []

    # Artifact dir must be writable
    artifacts_dir = paths.artifacts_dir
    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        probe = artifacts_dir / ".write_probe"
        probe.write_text("x")
        probe.unlink()
    except Exception as exc:
        issues.append(f"artifacts_dir not writable: {exc}")

    # DB must be openable
    try:
        conn = sqlite3.connect(paths.db_path)
        conn.execute("SELECT 1")
        conn.close()
    except Exception as exc:
        issues.append(f"db not reachable: {exc}")

    return issues


# ---------------------------------------------------------------------------
# Internal section checks
# ---------------------------------------------------------------------------

def _check_db(service: "AgenticOSService") -> dict[str, Any]:
    try:
        with service.db.connect() as conn:
            row_counts = {}
            for table in ("tasks", "approvals", "executions", "artifacts", "audit_events"):
                try:
                    (count,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    row_counts[table] = count
                except Exception:
                    row_counts[table] = None
        return {"reachable": True, "row_counts": row_counts}
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


def _check_audit_log(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "size_bytes": 0, "last_event_at": None}

    size = path.stat().st_size
    last_event_at: Optional[str] = None

    try:
        # Read the last non-empty line efficiently (up to 4 KB tail)
        with path.open("rb") as fh:
            fh.seek(max(0, size - 4096))
            tail = fh.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if line:
                event = json.loads(line)
                last_event_at = event.get("created_at")
                break
    except Exception:
        pass

    return {"exists": True, "size_bytes": size, "last_event_at": last_event_at}


def _check_artifacts_dir(path: Path) -> dict[str, Any]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("x")
        probe.unlink()
        return {"writable": True, "path": str(path)}
    except Exception as exc:
        return {"writable": False, "path": str(path), "error": str(exc)}


def _check_config(config: "AppConfig") -> dict[str, Any]:
    issues: list[str] = []
    paperclip_configured = config.paperclip is not None
    if not paperclip_configured:
        issues.append("Paperclip control plane is not configured")
    return {"paperclip_configured": paperclip_configured, "issues": issues}


def _check_paperclip_reachable(base_url: str) -> bool:
    """Attempt a lightweight GET to the Paperclip API health endpoint."""
    try:
        health_url = base_url.rstrip("/") + "/health"
        req = urllib.request.Request(health_url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            status = int(getattr(resp, "status", 0))
            return 200 <= status < 500
    except Exception:
        return False


def _get_last_reconcile_at(service: "AgenticOSService") -> Optional[str]:
    """
    Return the created_at timestamp of the most recent reconciler_ran audit event,
    or the mtime of the reconciler state file — whichever we can find first.
    """
    # Fast path: check state file mtime
    state_path = service.paths.data_dir / "paperclip_reconciler_state.json"
    if state_path.exists():
        try:
            mtime = state_path.stat().st_mtime
            return datetime.fromtimestamp(mtime, tz=timezone.utc).replace(microsecond=0).isoformat()
        except Exception:
            pass
    return None


def _get_paperclip_task_counts(service: "AgenticOSService") -> dict[str, int]:
    """Count tasks with and without a paperclip_issue_id."""
    try:
        with service.db.connect() as conn:
            (with_issue,) = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE paperclip_issue_id IS NOT NULL AND paperclip_issue_id != ''"
            ).fetchone()
            (without_issue,) = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE paperclip_issue_id IS NULL OR paperclip_issue_id = ''"
            ).fetchone()
        return {"with_issue": with_issue, "without_issue": without_issue}
    except Exception:
        return {"with_issue": 0, "without_issue": 0}


def _check_cron(repo_root: Path) -> dict[str, Any]:
    """Report scheduler state. Legacy cron/jobs.json is optional metadata only."""
    cron_path = repo_root / "cron" / "jobs.json"
    if not cron_path.exists():
        return {"jobs": [], "note": "internal scheduler active (no legacy cron file)"}

    try:
        data = json.loads(cron_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("_note"):
            return {"jobs": [], "note": str(data.get("_note"))}
        jobs_raw = data if isinstance(data, list) else data.get("jobs", [])
    except Exception as exc:
        return {"jobs": [], "error": str(exc)}

    jobs = []
    for job in jobs_raw:
        consecutive = job.get("consecutiveErrors", 0) or 0
        last_run = job.get("lastRunAt") or job.get("last_run_at")
        status = "ok"
        if consecutive >= 3:
            status = "error"
        elif consecutive >= 1:
            status = "warning"

        jobs.append(
            {
                "id": job.get("id", "unknown"),
                "enabled": job.get("enabled", True),
                "last_run_at": last_run,
                "consecutive_errors": consecutive,
                "status": status,
            }
        )

    return {"jobs": jobs}
