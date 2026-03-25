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
        "notion_token_set": true,
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

    # Notion token env var must be set (if notion is configured)
    if config.notion is not None:
        token = os.environ.get(config.notion.api_token_env)
        if not token:
            issues.append(
                f"Notion API token env var '{config.notion.api_token_env}' is not set"
            )

    for db_key, notion_cfg in config.notion_databases.items():
        token = os.environ.get(notion_cfg.api_token_env)
        if not token:
            issues.append(
                f"Notion DB '{db_key}': token env var '{notion_cfg.api_token_env}' is not set"
            )

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

    notion_token_set = False
    if config.notion is not None:
        token = os.environ.get(config.notion.api_token_env)
        notion_token_set = bool(token)
        if not notion_token_set:
            issues.append(f"Notion token env var '{config.notion.api_token_env}' not set")

    for db_key, notion_cfg in config.notion_databases.items():
        token = os.environ.get(notion_cfg.api_token_env)
        if not token:
            issues.append(
                f"Notion DB '{db_key}': token '{notion_cfg.api_token_env}' not set"
            )

    return {"notion_token_set": notion_token_set, "issues": issues}


def _check_paperclip_reachable(base_url: str) -> bool:
    """Attempt a lightweight GET to the Paperclip base URL. Returns True if reachable."""
    try:
        req = urllib.request.Request(base_url, method="GET")
        with urllib.request.urlopen(req, timeout=3):
            return True
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
    """
    Read the OpenClaw cron jobs.json (two levels up from the agentic-os repo)
    and report last-run/consecutive-error state for each job.
    """
    # The agentic-os repo lives at <openclaw_root>/workspace/agentic-os.
    # cron/jobs.json lives at <openclaw_root>/cron/jobs.json.
    cron_path = repo_root.parents[1] / "cron" / "jobs.json"
    if not cron_path.exists():
        return {"jobs": [], "note": "cron/jobs.json not found"}

    try:
        data = json.loads(cron_path.read_text(encoding="utf-8"))
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
