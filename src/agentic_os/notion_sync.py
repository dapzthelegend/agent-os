"""
DEPRECATED (Phase 6 — Paperclip cutover).

Bidirectional Notion sync — polls Notion DB and updates backend on manual status changes.

This module is no longer part of the live execution path. Operator-driven status changes
are now received via the Paperclip reconciler (paperclip_reconciler.py).
The `notion-sync` CLI command and `sync_notion_tasks()` service method remain available
for one-off use but are no longer called by any cron job.
Retained for rollback capability only — do not import from live code.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Optional

from .config import Paths, load_app_config, default_paths
from .notion import NotionAdapter
from .storage import Database
from .audit import AuditLog


NOTION_TO_BACKEND = {
    "Inbox": "new",
    "In progress": "in_progress",
    "Waiting": "awaiting_input",
    "Review": "awaiting_approval",
    "Planned": "approved",
    "Done": "executed",
    "Blocked": "failed",
    "Cancelled": "cancelled",
}


def sync_notion_to_backend(
    notion_adapter: NotionAdapter,
    db: Database,
    audit: AuditLog,
    *,
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Poll Notion DB for tasks where OpenClaw Task ID is set.
    Compare Notion status with backend status.
    If Notion status differs AND Notion page was edited after Last Agent Update,
    update backend task to match.
    
    Returns a summary dict.
    """
    results = {
        "synced_tasks": 0,
        "new_inbox_items": 0,
        "errors": [],
    }
    
    try:
        # Query all tasks in Notion where OpenClaw Task ID is NOT empty
        tasks = notion_adapter.query_tasks(limit=100)
        
        for notion_task in tasks:
            if notion_task.archived:
                continue
            
            # Skip if no backend task ID (will handle these as new items later)
            if not notion_task.backend_task_id:
                if notion_task.status == "Inbox":
                    results["new_inbox_items"] += 1
                    if verbose:
                        print(f"  [INFO] New inbox item in Notion: {notion_task.title} (page_id={notion_task.page_id})")
                continue
            
            # Load backend task
            try:
                backend_task = db.get_task(notion_task.backend_task_id)
            except KeyError:
                results["errors"].append(
                    f"Notion page {notion_task.page_id} references unknown backend task {notion_task.backend_task_id}"
                )
                continue
            
            # Compare statuses
            notion_status = notion_task.status
            backend_status = backend_task.status
            
            if not notion_status:
                continue
            
            # Map Notion status to backend status
            target_backend_status = NOTION_TO_BACKEND.get(notion_status)
            if target_backend_status is None:
                results["errors"].append(
                    f"Unknown Notion status '{notion_status}' on page {notion_task.page_id}"
                )
                continue
            
            # Check if they differ
            if target_backend_status == backend_status:
                if verbose:
                    print(f"  [OK] {notion_task.backend_task_id}: status in sync ({backend_status})")
                continue
            
            # Check if Notion page was edited after Last Agent Update
            # If not, agent wrote it more recently — don't override
            last_agent_update = notion_task.last_agent_update
            last_edited_time = notion_task.last_edited_time
            
            if last_agent_update and last_edited_time < last_agent_update:
                if verbose:
                    print(
                        f"  [SKIP] {notion_task.backend_task_id}: agent wrote more recently. "
                        f"(agent={last_agent_update}, notion={last_edited_time})"
                    )
                continue
            
            # Human edited the Notion status more recently. Update backend.
            if verbose:
                print(
                    f"  [SYNC] {notion_task.backend_task_id}: {backend_status} → {target_backend_status} "
                    f"(notion={last_edited_time}, agent={last_agent_update})"
                )
            
            db.update_task(
                notion_task.backend_task_id,
                status=target_backend_status,
            )
            
            # Append audit event
            event_id = audit.append(
                task_id=notion_task.backend_task_id,
                event_type="notion_sync",
                payload={
                    "notion_page_id": notion_task.page_id,
                    "notion_status": notion_status,
                    "backend_status_before": backend_status,
                    "backend_status_after": target_backend_status,
                },
                event_id=0,  # Placeholder; database will assign
            )
            audit.append(
                task_id=notion_task.backend_task_id,
                event_type="notion_sync",
                payload={
                    "notion_page_id": notion_task.page_id,
                    "notion_status": notion_status,
                    "backend_status_before": backend_status,
                    "backend_status_after": target_backend_status,
                },
                event_id=0,
            )
            
            results["synced_tasks"] += 1
    
    except Exception as exc:
        results["errors"].append(f"Sync failed: {exc}")
    
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync Notion DB changes to agentic-os backend",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep running and poll periodically",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Poll interval in seconds (default 300)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print detailed sync info",
    )
    
    args = parser.parse_args()
    
    # Load config and initialize
    paths = default_paths()
    config = load_app_config(paths)
    
    if config.notion is None:
        print("[ERROR] Notion not configured in agentic_os.config.json", file=sys.stderr)
        return 1
    
    db = Database(paths.db_path)
    audit = AuditLog(paths.audit_log_path)
    adapter = NotionAdapter(config.notion)
    
    # Run sync once, or repeatedly if --watch
    if args.watch:
        if args.verbose:
            print("[INFO] Notion sync started (watch mode)")
        try:
            while True:
                results = sync_notion_to_backend(adapter, db, audit, verbose=args.verbose)
                if args.verbose:
                    print(f"[INFO] Sync complete: {json.dumps(results)}")
                if results["errors"]:
                    for err in results["errors"]:
                        print(f"[WARN] {err}")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            if args.verbose:
                print("[INFO] Notion sync stopped")
            return 0
    else:
        results = sync_notion_to_backend(adapter, db, audit, verbose=args.verbose)
        if args.verbose or results["errors"]:
            print(json.dumps(results, indent=2))
        if results["errors"]:
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
