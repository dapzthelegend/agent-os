from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional

from .config import default_paths
from .models import ACTION_SOURCES, DOMAINS, OperatorError, RequestClassification, STATUSES
from .notion import NotionError
from .openclaw_bridge import normalize_openclaw_daily_routine_payload
from .service import AgenticOSService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Week 4 operator CLI for agentic-os")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize local storage")

    request_parser = subparsers.add_parser("request", help="Request operations")
    request_subparsers = request_parser.add_subparsers(dest="request_command", required=True)
    create_request_parser(request_subparsers)

    task_parser = subparsers.add_parser("task", help="Task inspection and state")
    task_subparsers = task_parser.add_subparsers(dest="task_command", required=True)
    create_task_list_parser(task_subparsers)
    create_task_show_parser(task_subparsers)
    create_task_trace_parser(task_subparsers)
    create_task_complete_parser(task_subparsers)
    create_task_fail_parser(task_subparsers)
    create_task_execute_parser(task_subparsers)
    create_task_list_ready_parser(task_subparsers)
    create_task_resolve_paperclip_parser(task_subparsers)
    create_task_pickup_parser(task_subparsers)
    create_task_mark_dispatched_parser(task_subparsers)
    create_task_record_result_parser(task_subparsers)
    create_task_submit_plan_parser(task_subparsers)
    create_task_requeue_parser(task_subparsers)

    approval_parser = subparsers.add_parser("approval", help="Approval operations")
    approval_subparsers = approval_parser.add_subparsers(dest="approval_command", required=True)
    create_approval_list_parser(approval_subparsers)
    create_approval_show_parser(approval_subparsers)
    create_approval_approve_parser(approval_subparsers)
    create_approval_deny_parser(approval_subparsers)
    create_approval_cancel_parser(approval_subparsers)
    create_approval_remind_pending_parser(approval_subparsers)

    execution_parser = subparsers.add_parser("execution", help="Execution inspection")
    execution_subparsers = execution_parser.add_subparsers(dest="execution_command", required=True)
    create_execution_show_parser(execution_subparsers)

    audit_parser = subparsers.add_parser("audit", help="Audit inspection")
    audit_subparsers = audit_parser.add_subparsers(dest="audit_command", required=True)
    create_audit_tail_parser(audit_subparsers)

    recap_parser = subparsers.add_parser("recap", help="Simple recap commands over durable state")
    recap_subparsers = recap_parser.add_subparsers(dest="recap_command", required=True)
    create_recap_today_parser(recap_subparsers)
    create_recap_approvals_parser(recap_subparsers)
    create_recap_drafts_parser(recap_subparsers)
    create_recap_failures_parser(recap_subparsers)
    create_recap_external_actions_parser(recap_subparsers)
    create_recap_overdue_parser(recap_subparsers)
    create_recap_in_progress_parser(recap_subparsers)

    artifact_parser = subparsers.add_parser("artifact", help="Artifact operations")
    artifact_subparsers = artifact_parser.add_subparsers(dest="artifact_command", required=True)
    create_artifact_revise_parser(artifact_subparsers)

    openclaw_parser = subparsers.add_parser("openclaw", help="Record OpenClaw-mediated operations")
    openclaw_subparsers = openclaw_parser.add_subparsers(dest="openclaw_command", required=True)
    create_openclaw_read_parser(openclaw_subparsers)
    create_openclaw_draft_parser(openclaw_subparsers)
    create_openclaw_execution_parser(openclaw_subparsers)
    create_openclaw_daily_routine_parser(openclaw_subparsers)

    adapter_parser = subparsers.add_parser("adapter", help="Future custom-adapter seam")
    adapter_subparsers = adapter_parser.add_subparsers(dest="adapter_command", required=True)
    create_adapter_execute_parser(adapter_subparsers)

    notion_parser = subparsers.add_parser("notion", help="Thin Notion adapter operations")
    notion_subparsers = notion_parser.add_subparsers(dest="notion_command", required=True)
    create_notion_create_parser(notion_subparsers)
    create_notion_query_parser(notion_subparsers)
    create_notion_sync_parser(notion_subparsers)
    create_notion_get_parser(notion_subparsers)
    create_notion_update_status_parser(notion_subparsers)
    create_notion_append_note_parser(notion_subparsers)

    daily_routine_parser = subparsers.add_parser("daily-routine", help="Phase 1 daily routine support flow")
    daily_routine_subparsers = daily_routine_parser.add_subparsers(dest="daily_routine_command", required=True)
    create_daily_routine_run_parser(daily_routine_subparsers)

    calendar_parser = subparsers.add_parser("calendar", help="Google Calendar operations (agent inbox)")
    calendar_subparsers = calendar_parser.add_subparsers(dest="calendar_command", required=True)
    create_calendar_list_parser(calendar_subparsers)
    create_calendar_create_parser(calendar_subparsers)
    create_calendar_block_parser(calendar_subparsers)
    create_calendar_update_parser(calendar_subparsers)
    create_calendar_delete_parser(calendar_subparsers)
    create_calendar_remind_parser(calendar_subparsers)

    retry_parser = subparsers.add_parser("retry", help="Retry a stalled or failed task")
    retry_parser.add_argument("task_id")
    retry_parser.add_argument("--feedback", default="operator retry")

    subparsers.add_parser("health", help="Print system health snapshot")

    stall_parser = subparsers.add_parser("stall-check", help="Scan and flag stalled tasks, send Discord alerts")
    stall_parser.add_argument("--threshold-hours", type=float, default=2.0)

    return parser


def create_request_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("create", help="Store, classify, and policy-evaluate a request")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--intent", required=True, dest="intent_type")
    parser.add_argument("--risk", required=True, dest="risk_level")
    parser.add_argument("--request", required=True, dest="user_request")
    parser.add_argument("--target")
    parser.add_argument("--metadata-json")
    parser.add_argument("--external-write", action="store_true")
    parser.add_argument("--operation-key")
    parser.add_argument("--result-summary")
    parser.add_argument("--external-ref")
    parser.add_argument("--artifact-type")
    parser.add_argument("--artifact-text")
    parser.add_argument("--artifact-json")


def create_task_list_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("list", help="List recent tasks")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--status", choices=STATUSES)
    parser.add_argument("--domain", choices=DOMAINS)
    parser.add_argument("--target")
    parser.add_argument("--action-source", choices=ACTION_SOURCES)


def create_task_show_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("show", help="Show task detail with approvals, artifacts, execution, and audit")
    parser.add_argument("task_id")


def create_task_trace_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("trace", help="Show task, audit, artifact, approval, and execution trace")
    parser.add_argument("task_id")


def create_task_complete_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("complete", help="Mark a task completed")
    parser.add_argument("task_id")
    parser.add_argument("--result-summary", required=True)


def create_task_fail_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("fail", help="Mark a task failed")
    parser.add_argument("task_id")
    parser.add_argument("--reason", required=True)


def create_task_execute_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("execute", help="Record an executed action with idempotency")
    parser.add_argument("task_id")
    parser.add_argument("--result-summary", required=True)
    parser.add_argument("--tool-name")
    parser.add_argument("--tool-result-json")


# ── Execution loop commands ───────────────────────────────────────────────────

def create_task_list_ready_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "list-ready", help="List tasks eligible for execution (approved or new+read_ok)"
    )
    parser.add_argument("--limit", type=int, default=20)


def create_task_resolve_paperclip_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "resolve-by-paperclip-issue",
        help="Resolve (or import) a backend task for a Paperclip issue ID — for runtimes started by routines",
    )
    parser.add_argument("--paperclip-issue-id", required=True, help="Paperclip issue ID")


def create_task_pickup_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "pickup", help="Atomically claim a task for execution (→ in_progress)"
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--claimed-by", default="task_executor_cron")


def create_task_mark_dispatched_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "mark-dispatched", help="Record that a child session was spawned for a task"
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--session-key", required=True)
    parser.add_argument("--agent", required=True)


def create_task_record_result_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "record-result",
        help="Record a child session's execution result from an output file",
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--output-file", required=True, help="Path to file containing full agent output")
    parser.add_argument("--session-key", default="unknown")


def create_task_submit_plan_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "submit-plan",
        help="Submit a plan from a file and transition task to awaiting_plan_review",
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--plan-file", required=True, help="Path to file containing plan text")
    parser.add_argument("--session-key", default="unknown")
    parser.add_argument("--doc-id", default=None, help="Optional Paperclip document id for correlation")


def create_task_requeue_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "requeue", help="Requeue a stalled/failed task back to a ready state"
    )
    parser.add_argument("--task-id", required=True)

# ─────────────────────────────────────────────────────────────────────────────


def create_approval_list_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("list", help="List approvals")
    parser.add_argument("--task-id")


def create_approval_show_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("show", help="Show approval detail")
    parser.add_argument("approval_id")


def create_approval_approve_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("approve", help="Approve a pending approval record")
    parser.add_argument("approval_id")
    parser.add_argument("--note")


def create_approval_deny_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("deny", help="Deny a pending approval record")
    parser.add_argument("approval_id")
    parser.add_argument("--note")


def create_approval_cancel_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("cancel", help="Cancel a pending approval record")
    parser.add_argument("approval_id")
    parser.add_argument("--note")


def create_approval_remind_pending_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "remind-pending",
        help="Send reminders for approvals pending longer than --threshold-hours (default 1h)",
    )
    parser.add_argument("--threshold-hours", type=float, default=1.0)


def create_artifact_revise_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("revise", help="Create a new artifact version for a task")
    parser.add_argument("task_id")
    parser.add_argument("--artifact-type")
    parser.add_argument("--artifact-text")
    parser.add_argument("--artifact-json")


def create_openclaw_read_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("read", help="Record a completed OpenClaw-backed read")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--risk", required=True, dest="risk_level")
    parser.add_argument("--request", required=True, dest="user_request")
    parser.add_argument("--tool-name", required=True)
    parser.add_argument("--tool-input-json")
    parser.add_argument("--tool-result-json")
    parser.add_argument("--summary", required=True)
    parser.add_argument("--target")
    parser.add_argument("--metadata-json")
    parser.add_argument("--artifact-type")
    parser.add_argument("--artifact-text")
    parser.add_argument("--artifact-json")
    parser.add_argument("--action-source", default="openclaw_tool")


def create_openclaw_draft_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("draft", help="Record an OpenClaw-generated draft artifact")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--risk", required=True, dest="risk_level")
    parser.add_argument("--request", required=True, dest="user_request")
    parser.add_argument("--artifact-type", required=True)
    parser.add_argument("--artifact-text")
    parser.add_argument("--artifact-json")
    parser.add_argument("--tool-name")
    parser.add_argument("--tool-input-json")
    parser.add_argument("--summary")
    parser.add_argument("--target")
    parser.add_argument("--metadata-json")
    parser.add_argument("--action-source", default="openclaw_skill")


def create_openclaw_execution_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "execution",
        help="Record an OpenClaw-mediated approval-backed execution request",
    )
    parser.add_argument("--domain", required=True)
    parser.add_argument("--risk", required=True, dest="risk_level")
    parser.add_argument("--request", required=True, dest="user_request")
    parser.add_argument("--tool-name", required=True)
    parser.add_argument("--operation-key", required=True)
    parser.add_argument("--result-summary", required=True)
    parser.add_argument("--tool-input-json")
    parser.add_argument("--tool-result-json")
    parser.add_argument("--target")
    parser.add_argument("--metadata-json")
    parser.add_argument("--artifact-type")
    parser.add_argument("--artifact-text")
    parser.add_argument("--artifact-json")
    parser.add_argument("--action-source", default="openclaw_tool")


def create_openclaw_daily_routine_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "daily-routine",
        help="Normalize OpenClaw summary payloads and run the daily routine flow",
    )
    parser.add_argument("--input-file")
    parser.add_argument("--input-json")
    parser.add_argument("--date")
    parser.add_argument("--timezone")
    parser.add_argument("--recipient")
    parser.add_argument("--delivery-time")
    parser.add_argument("--no-notion", action="store_true")
    parser.add_argument("--print-normalized", action="store_true")
    parser.add_argument("--dry-run", action="store_true")


def create_adapter_execute_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("execute", help="Call the future custom-adapter seam")
    parser.add_argument("--adapter-name", required=True)
    parser.add_argument("--action-name", required=True)


def create_notion_create_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("create-task", help="Create a Notion task and persist the backend external_ref")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--risk", required=True, dest="risk_level")
    parser.add_argument("--request", required=True, dest="user_request")
    parser.add_argument("--title", required=True)
    parser.add_argument("--status")
    parser.add_argument("--task-type")
    parser.add_argument("--area")
    parser.add_argument("--target", default="notion_task")
    parser.add_argument("--metadata-json")
    parser.add_argument("--operation-key")


def create_notion_query_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("query-tasks", help="Query Notion tasks by status and/or updated timestamp")
    parser.add_argument("--request", default="Query Notion tasks")
    parser.add_argument("--status")
    parser.add_argument("--updated-since")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--domain", default="technical")
    parser.add_argument("--risk", default="low", dest="risk_level")
    parser.add_argument("--metadata-json")


def create_notion_sync_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "sync-tasks",
        help="Import Notion tasks into backend durable state for heartbeat/cron intake",
    )
    parser.add_argument("--request", default="Sync Notion tasks into backend durable state")
    parser.add_argument(
        "--status",
        action="append",
        default=[],
        help="Notion status filter to sync (repeatable, default: Inbox)",
    )
    parser.add_argument("--updated-since")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--domain", default="system")
    parser.add_argument("--risk", default="low", dest="risk_level")
    parser.add_argument("--target", default="notion_task_sync")
    parser.add_argument("--metadata-json")


def create_notion_get_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("get-task", help="Fetch a single Notion task by page id")
    parser.add_argument("page_id")
    parser.add_argument("--request", default="Fetch Notion task detail")
    parser.add_argument("--domain", default="technical")
    parser.add_argument("--risk", default="low", dest="risk_level")
    parser.add_argument("--metadata-json")


def create_notion_update_status_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("update-status", help="Push a backend task status to its linked Notion page")
    parser.add_argument("task_id")
    parser.add_argument("--backend-status", required=True, choices=STATUSES)
    parser.add_argument("--page-id")
    parser.add_argument("--note")


def create_notion_append_note_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("append-note", help="Append a short agent note to a linked Notion page")
    parser.add_argument("task_id")
    parser.add_argument("--page-id")
    parser.add_argument("--note", required=True)


def create_calendar_list_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("list", help="List today's events from all calendars")
    parser.add_argument("--date", help="ISO date to poll (default: today)")
    parser.add_argument("--account", choices=["agent", "dapz", "sola", "all"], default="all")


def create_calendar_create_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("create", help="Create a calendar event (agent inbox only)")
    parser.add_argument("--title", required=True)
    parser.add_argument("--start", required=True, help="ISO 8601 datetime, e.g. 2026-03-24T10:00:00")
    parser.add_argument("--end", required=True, help="ISO 8601 datetime, e.g. 2026-03-24T11:00:00")
    parser.add_argument("--description")
    parser.add_argument("--location")
    parser.add_argument("--attendees", nargs="*", help="Space-separated attendee emails")
    parser.add_argument(
        "--reminders",
        nargs="*",
        type=int,
        metavar="MINUTES",
        help="Popup reminder times in minutes before event, e.g. --reminders 10 30",
    )
    parser.add_argument("--timezone", default="Europe/London")


def create_calendar_block_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("block", help="Block time on the agent calendar")
    parser.add_argument("--title", required=True)
    parser.add_argument("--start", required=True, help="ISO 8601 datetime")
    parser.add_argument("--end", required=True, help="ISO 8601 datetime")
    parser.add_argument("--description")
    parser.add_argument("--timezone", default="Europe/London")


def create_calendar_update_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("update", help="Update an existing calendar event")
    parser.add_argument("event_id")
    parser.add_argument("--title")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--description")
    parser.add_argument("--location")
    parser.add_argument(
        "--reminders",
        nargs="*",
        type=int,
        metavar="MINUTES",
    )
    parser.add_argument("--timezone", default="Europe/London")


def create_calendar_delete_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("delete", help="Delete a calendar event")
    parser.add_argument("event_id")


def create_calendar_remind_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("remind", help="Add a popup reminder to an existing event")
    parser.add_argument("event_id")
    parser.add_argument("--minutes", type=int, required=True, help="Minutes before event")


def create_daily_routine_run_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("run", help="Build and persist a daily recap from structured inputs")
    parser.add_argument("--input-file")
    parser.add_argument("--input-json")
    parser.add_argument("--date")
    parser.add_argument("--timezone")
    parser.add_argument("--recipient")
    parser.add_argument("--delivery-time")
    parser.add_argument("--calendar-file")
    parser.add_argument("--calendar-json")
    parser.add_argument("--personal-inbox-file")
    parser.add_argument("--personal-inbox-json")
    parser.add_argument("--agent-inbox-file")
    parser.add_argument("--agent-inbox-json")
    parser.add_argument("--notion-file")
    parser.add_argument("--notion-json")
    parser.add_argument("--no-notion", action="store_true")
    parser.add_argument(
        "--poll-inbox",
        action="store_true",
        help="Fetch agent and personal inboxes from Gmail API (overrides --*-inbox-* flags)",
    )
    parser.add_argument(
        "--poll-calendar",
        action="store_true",
        help="Fetch today's events from Google Calendar API (overrides --calendar-* flags)",
    )
    parser.add_argument(
        "--send-email",
        action="store_true",
        help="Send the recap email via Gmail API after building it",
    )


def create_execution_show_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("show", help="Show execution detail by operation_key")
    parser.add_argument("operation_key")


def create_audit_tail_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("tail", help="Show recent audit activity")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--domain", choices=DOMAINS)
    parser.add_argument("--target")


def create_recap_today_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("today", help="Summarize what happened today")
    parser.add_argument("--domain", choices=DOMAINS)


def create_recap_approvals_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("approvals", help="Summarize work awaiting approval")
    parser.add_argument("--domain", choices=DOMAINS)


def create_recap_drafts_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("drafts", help="Summarize open drafts")
    parser.add_argument("--domain", choices=DOMAINS)


def create_recap_failures_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("failures", help="Summarize recent failures")
    parser.add_argument("--domain", choices=DOMAINS)
    parser.add_argument("--limit", type=int, default=20)


def create_recap_external_actions_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("external-actions", help="Summarize recorded external actions")
    parser.add_argument("--domain", choices=DOMAINS)
    parser.add_argument("--limit", type=int, default=20)


def create_recap_overdue_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("overdue", help="List tasks with no update for >N hours (default 48)")
    parser.add_argument("--domain", choices=DOMAINS)
    parser.add_argument("--threshold-hours", type=float, default=48.0)


def create_recap_in_progress_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("in-progress", help="List all in-progress and awaiting tasks")
    parser.add_argument("--domain", choices=DOMAINS)


def parse_artifact(args: argparse.Namespace) -> Optional[Any]:
    if getattr(args, "artifact_json", None) and getattr(args, "artifact_text", None):
        raise SystemExit("provide either --artifact-json or --artifact-text, not both")
    if getattr(args, "artifact_json", None):
        return json.loads(args.artifact_json)
    if getattr(args, "artifact_text", None):
        return args.artifact_text
    return None


def parse_metadata_json(raw_value: Optional[str]) -> Optional[dict[str, Any]]:
    if raw_value is None:
        return None
    value = json.loads(raw_value)
    if not isinstance(value, dict):
        raise SystemExit("--metadata-json must decode to a JSON object")
    return value


def parse_optional_json_object(raw_value: Optional[str], *, flag_name: str) -> Optional[dict[str, Any]]:
    if raw_value is None:
        return None
    value = json.loads(raw_value)
    if not isinstance(value, dict):
        raise SystemExit(f"{flag_name} must decode to a JSON object")
    return value


def load_json_object(
    *,
    raw_value: Optional[str],
    file_path: Optional[str],
    flag_name: str,
) -> Optional[dict[str, Any]]:
    if raw_value and file_path:
        raise SystemExit(f"provide either inline JSON or a file for {flag_name}, not both")
    if raw_value:
        value = json.loads(raw_value)
    elif file_path:
        value = json.loads(Path(file_path).read_text(encoding="utf-8"))
    else:
        return None
    if not isinstance(value, dict):
        raise SystemExit(f"{flag_name} must decode to a JSON object")
    return value


def build_daily_routine_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.input_file and args.input_json:
        raise SystemExit("provide either --input-file or --input-json, not both")
    base_payload = load_json_object(
        raw_value=args.input_json,
        file_path=args.input_file,
        flag_name="--input-file/--input-json",
    ) or {}
    for key, value in (
        ("date", args.date),
        ("timezone", args.timezone),
        ("recipient", args.recipient),
        ("delivery_time", args.delivery_time),
    ):
        if value is not None:
            base_payload[key] = value

    # Gmail API polling (overrides manual --*-inbox-* flags when set)
    if getattr(args, "poll_inbox", False):
        from .gmail_poller import poll_all_inboxes
        polled = poll_all_inboxes()
        base_payload.setdefault("agent_inbox", polled["agent_inbox"])
        base_payload.setdefault("personal_inbox", polled["personal_inbox"])

    # Google Calendar API polling (overrides --calendar-* flags when set)
    if getattr(args, "poll_calendar", False):
        from .calendar_poller import poll_calendar
        base_payload.setdefault("calendar", poll_calendar())

    section_inputs = {
        "calendar": load_json_object(
            raw_value=args.calendar_json,
            file_path=args.calendar_file,
            flag_name="calendar",
        ),
        "personal_inbox": load_json_object(
            raw_value=args.personal_inbox_json,
            file_path=args.personal_inbox_file,
            flag_name="personal inbox",
        ),
        "agent_inbox": load_json_object(
            raw_value=args.agent_inbox_json,
            file_path=args.agent_inbox_file,
            flag_name="agent inbox",
        ),
        "notion": load_json_object(
            raw_value=args.notion_json,
            file_path=args.notion_file,
            flag_name="notion",
        ),
    }
    for key, value in section_inputs.items():
        if value is not None:
            base_payload[key] = value
    return base_payload


def build_openclaw_daily_routine_payload(args: argparse.Namespace) -> dict[str, Any]:
    raw_payload = load_json_object(
        raw_value=args.input_json,
        file_path=args.input_file,
        flag_name="--input-file/--input-json",
    )
    if raw_payload is None:
        raise SystemExit("provide --input-file or --input-json")
    payload = normalize_openclaw_daily_routine_payload(raw_payload)
    for key, value in (
        ("date", args.date),
        ("timezone", args.timezone),
        ("recipient", args.recipient),
        ("delivery_time", args.delivery_time),
    ):
        if value is not None:
            payload[key] = value
    return payload


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    return value


def print_json(payload: Any) -> None:
    print(json.dumps(to_jsonable(payload), indent=2, sort_keys=True))


def print_correlation_trace(service: AgenticOSService, task_id: str) -> None:
    """Print a structured correlation chain: task → approval → execution → result."""
    detail = service.get_task_detail(task_id)
    task = detail["task"]
    approvals = detail["approvals"]
    execution = detail["execution"]
    audit_events = detail["audit_events"]

    lines = []
    lines.append(f"=== Correlation Trace: {task_id} ===")
    lines.append("")

    # Task node
    lines.append(f"TASK  {task['id']}")
    lines.append(f"      status       : {task['status']}")
    lines.append(f"      domain       : {task['domain']}")
    lines.append(f"      intent       : {task['intent_type']}")
    lines.append(f"      risk         : {task['risk_level']}")
    lines.append(f"      policy       : {task['policy_decision']}")
    lines.append(f"      operation_key: {task['operation_key'] or '—'}")
    lines.append(f"      created_at   : {task['created_at']}")
    lines.append(f"      updated_at   : {task['updated_at']}")
    lines.append("")

    # Approval node(s)
    if approvals:
        for i, approval in enumerate(approvals):
            prefix = "APPROVAL" if i == 0 else "        "
            lines.append(f"{prefix}  {approval['id']}")
            lines.append(f"          status      : {approval['status']}")
            lines.append(f"          subject_type: {approval['subject_type']}")
            lines.append(f"          operation_key: {approval.get('operation_key') or '—'}")
            if approval.get("decision_note"):
                lines.append(f"          note        : {approval['decision_note']}")
            lines.append(f"          created_at  : {approval['created_at']}")
            if approval.get("decided_at"):
                lines.append(f"          decided_at  : {approval['decided_at']}")
    else:
        lines.append("APPROVAL  — (none)")
    lines.append("")

    # Execution node
    if execution:
        lines.append(f"EXECUTION  {execution['operation_key']}")
        lines.append(f"           task_id    : {execution['task_id']}")
        lines.append(f"           approval_id: {execution.get('approval_id') or '—'}")
        lines.append(f"           status     : {execution['status']}")
        if execution.get("result_summary"):
            summary = execution["result_summary"][:120].replace("\n", " ")
            lines.append(f"           result     : {summary}")
        lines.append(f"           created_at : {execution['created_at']}")
    else:
        lines.append("EXECUTION  — (not yet executed)")
    lines.append("")

    # Result
    if task.get("result_summary"):
        summary = task["result_summary"][:200].replace("\n", " ")
        lines.append(f"RESULT     {summary}")
    elif task.get("artifact_ref"):
        lines.append(f"ARTIFACT   {task['artifact_ref']}")
    else:
        lines.append("RESULT     — (none)")
    lines.append("")

    # Audit chain
    lines.append(f"AUDIT TRAIL  ({len(audit_events)} events)")
    for event in audit_events:
        import json as _json
        payload_str = ""
        try:
            p = _json.loads(event["payload_json"]) if isinstance(event.get("payload_json"), str) else event.get("payload", {})
            # Surface correlation IDs in audit output
            corr_parts = []
            for key in ("task_id", "execution_id", "approval_id"):
                if p.get(key):
                    corr_parts.append(f"{key}={p[key]}")
            payload_str = "  [" + ", ".join(corr_parts) + "]" if corr_parts else ""
        except Exception:
            pass
        lines.append(f"  [{event['created_at']}] {event['event_type']}{payload_str}")

    print("\n".join(lines))


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = AgenticOSService(default_paths())

    try:
        if args.command == "init":
            service.initialize()
            print_json(
                {
                    "status": "initialized",
                    "db_path": str(service.paths.db_path),
                    "audit_log_path": str(service.paths.audit_log_path),
                    "artifacts_dir": str(service.paths.artifacts_dir),
                    "policy_rules_path": str(service.paths.policy_rules_path),
                }
            )
            return 0

        if args.command == "openclaw" and args.openclaw_command == "daily-routine" and args.dry_run:
            print_json({"normalized_payload": build_openclaw_daily_routine_payload(args)})
            return 0

        service.initialize()

        if args.command == "request" and args.request_command == "create":
            classification = RequestClassification(
                domain=args.domain,
                intent_type=args.intent_type,
                risk_level=args.risk_level,
            ).validate()
            artifact_content = parse_artifact(args)
            payload = service.create_request(
                user_request=args.user_request,
                classification=classification,
                target=args.target,
                request_metadata=parse_metadata_json(args.metadata_json),
                external_write=args.external_write,
                operation_key=args.operation_key,
                artifact_type=args.artifact_type,
                artifact_content=artifact_content,
                result_summary=args.result_summary,
                external_ref=args.external_ref,
                action_source="manual",
            )
            print_json(payload)
            return 0

        if args.command == "openclaw" and args.openclaw_command == "read":
            classification = RequestClassification(
                domain=args.domain,
                intent_type="read",
                risk_level=args.risk_level,
            ).validate()
            payload = service.record_openclaw_read(
                user_request=args.user_request,
                classification=classification,
                tool_name=args.tool_name,
                tool_input=parse_optional_json_object(args.tool_input_json, flag_name="--tool-input-json"),
                tool_result=parse_optional_json_object(args.tool_result_json, flag_name="--tool-result-json"),
                summary=args.summary,
                target=args.target,
                request_metadata=parse_metadata_json(args.metadata_json),
                artifact_type=args.artifact_type,
                artifact_content=parse_artifact(args),
                action_source=args.action_source,
            )
            print_json(payload)
            return 0

        if args.command == "openclaw" and args.openclaw_command == "draft":
            artifact_content = parse_artifact(args)
            if artifact_content is None:
                raise SystemExit("provide --artifact-json or --artifact-text")
            classification = RequestClassification(
                domain=args.domain,
                intent_type="draft",
                risk_level=args.risk_level,
            ).validate()
            payload = service.record_openclaw_draft(
                user_request=args.user_request,
                classification=classification,
                draft_artifact=artifact_content,
                artifact_type=args.artifact_type,
                tool_name=args.tool_name,
                tool_input=parse_optional_json_object(args.tool_input_json, flag_name="--tool-input-json"),
                summary=args.summary,
                target=args.target,
                request_metadata=parse_metadata_json(args.metadata_json),
                action_source=args.action_source,
            )
            print_json(payload)
            return 0

        if args.command == "openclaw" and args.openclaw_command == "execution":
            classification = RequestClassification(
                domain=args.domain,
                intent_type="execute",
                risk_level=args.risk_level,
            ).validate()
            payload = service.record_openclaw_execution(
                user_request=args.user_request,
                classification=classification,
                tool_name=args.tool_name,
                operation_key=args.operation_key,
                result_summary=args.result_summary,
                tool_input=parse_optional_json_object(args.tool_input_json, flag_name="--tool-input-json"),
                tool_result=parse_optional_json_object(args.tool_result_json, flag_name="--tool-result-json"),
                target=args.target,
                request_metadata=parse_metadata_json(args.metadata_json),
                artifact_type=args.artifact_type,
                artifact_content=parse_artifact(args),
                action_source=args.action_source,
            )
            print_json(payload)
            return 0

        if args.command == "openclaw" and args.openclaw_command == "daily-routine":
            normalized_payload = build_openclaw_daily_routine_payload(args)
            if args.dry_run:
                print_json({"normalized_payload": normalized_payload})
                return 0
            result = service.run_daily_routine(
                payload=normalized_payload,
                create_notion_tasks=not args.no_notion,
            )
            if args.print_normalized:
                print_json({"normalized_payload": normalized_payload, **result})
            else:
                print_json(result)
            return 0

        if args.command == "task" and args.task_command == "list":
            tasks = service.list_tasks(
                limit=args.limit,
                status=args.status,
                domain=args.domain,
                target=args.target,
                action_source=args.action_source,
            )
            payload = {
                "filters": {
                    "limit": args.limit,
                    "status": args.status,
                    "domain": args.domain,
                    "target": args.target,
                    "action_source": args.action_source,
                },
                "tasks": [asdict(task) for task in tasks],
            }
            print_json(payload)
            return 0

        if args.command == "task" and args.task_command == "show":
            print_json(service.get_task_detail(args.task_id))
            return 0

        if args.command == "task" and args.task_command == "trace":
            print_correlation_trace(service, args.task_id)
            return 0

        if args.command == "task" and args.task_command == "complete":
            task = service.complete_task(args.task_id, args.result_summary)
            print_json({"task": asdict(task)})
            return 0

        if args.command == "task" and args.task_command == "fail":
            task = service.fail_task(args.task_id, args.reason)
            print_json({"task": asdict(task)})
            return 0

        if args.command == "task" and args.task_command == "execute":
            print_json(
                service.execute_action(
                    args.task_id,
                    args.result_summary,
                    tool_name=args.tool_name,
                    tool_result=parse_optional_json_object(
                        args.tool_result_json,
                        flag_name="--tool-result-json",
                    ),
                )
            )
            return 0

        # ── Execution loop commands ───────────────────────────────────────────

        if args.command == "task" and args.task_command == "list-ready":
            tasks = service.list_ready_tasks(limit=args.limit)
            print_json({"count": len(tasks), "tasks": [asdict(t) for t in tasks]})
            return 0

        if args.command == "task" and args.task_command == "resolve-by-paperclip-issue":
            try:
                result = service.ensure_task_for_paperclip_issue(args.paperclip_issue_id)
                print_json(result)
                return 0
            except RuntimeError as exc:
                print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
                return 1

        if args.command == "task" and args.task_command == "pickup":
            result = service.pickup_task(args.task_id, claimed_by=args.claimed_by)
            print_json(result)
            return 0 if result["success"] else 1

        if args.command == "task" and args.task_command == "mark-dispatched":
            service.mark_dispatched(args.task_id, session_key=args.session_key, agent=args.agent)
            print_json({"status": "ok", "task_id": args.task_id, "session_key": args.session_key})
            return 0

        if args.command == "task" and args.task_command == "record-result":
            from pathlib import Path as _Path
            from .execution_receiver import receive_execution_result, ExecutionParseError
            from .config import default_paths as _default_paths
            output_path = _Path(args.output_file)
            if not output_path.exists():
                import sys as _sys
                print(
                    json.dumps({"status": "error", "error": f"output_file not found: {args.output_file}"}),
                    file=_sys.stderr,
                )
                return 1
            raw_output = output_path.read_text(encoding="utf-8")
            paths = _default_paths()
            try:
                result = receive_execution_result(
                    raw_output,
                    task_id=args.task_id,
                    session_key=args.session_key,
                    paths=paths,
                )
            except ExecutionParseError as exc:
                import sys as _sys
                print(
                    json.dumps({"status": "error", "task_id": args.task_id, "error": str(exc)}),
                    file=_sys.stderr,
                )
                return 1
            if result.success:
                print_json({
                    "status": "already_done" if result.idempotent else "success",
                    "task_id": result.task_id,
                    "artifact_id": result.artifact_id,
                })
                return 0
            import sys as _sys
            print(
                json.dumps({"status": "error", "task_id": result.task_id, "error": result.error}),
                file=_sys.stderr,
            )
            return 1

        if args.command == "task" and args.task_command == "submit-plan":
            plan_path = Path(args.plan_file)
            if not plan_path.exists():
                import sys as _sys
                print(
                    json.dumps({"status": "error", "error": f"plan_file not found: {args.plan_file}"}),
                    file=_sys.stderr,
                )
                return 1
            plan_text = plan_path.read_text(encoding="utf-8").strip()
            if not plan_text:
                import sys as _sys
                print(
                    json.dumps({"status": "error", "error": "plan file is empty"}),
                    file=_sys.stderr,
                )
                return 1
            task = service.submit_plan(args.task_id, plan_text)
            payload = {
                "status": "ok",
                "task_id": task.id,
                "plan_version": task.plan_version,
                "session_key": args.session_key,
            }
            if args.doc_id:
                payload["paperclip_document_id"] = args.doc_id
            print_json(payload)
            return 0

        if args.command == "task" and args.task_command == "requeue":
            task = service.requeue_task(args.task_id)
            print_json({"task": asdict(task)})
            return 0

        # ─────────────────────────────────────────────────────────────────────

        if args.command == "approval" and args.approval_command == "list":
            payload = {"approvals": [asdict(item) for item in service.list_approvals(task_id=args.task_id)]}
            print_json(payload)
            return 0

        if args.command == "approval" and args.approval_command == "show":
            print_json(service.get_approval_detail(args.approval_id))
            return 0

        if args.command == "approval" and args.approval_command == "approve":
            print_json(service.approve(args.approval_id, decision_note=args.note))
            return 0

        if args.command == "approval" and args.approval_command == "deny":
            print_json(service.deny(args.approval_id, decision_note=args.note))
            return 0

        if args.command == "approval" and args.approval_command == "cancel":
            print_json(service.cancel(args.approval_id, decision_note=args.note))
            return 0

        if args.command == "approval" and args.approval_command == "remind-pending":
            print_json(service.send_approval_reminders(threshold_hours=args.threshold_hours))
            return 0

        if args.command == "artifact" and args.artifact_command == "revise":
            artifact_content = parse_artifact(args)
            if artifact_content is None:
                raise SystemExit("provide --artifact-json or --artifact-text")
            print_json(
                service.revise_artifact(
                    args.task_id,
                    artifact_type=args.artifact_type,
                    artifact_content=artifact_content,
                )
            )
            return 0

        if args.command == "execution" and args.execution_command == "show":
            print_json(service.get_execution_detail(args.operation_key))
            return 0

        if args.command == "audit" and args.audit_command == "tail":
            print_json(
                service.list_recent_audit_activity(
                    limit=args.limit,
                    domain=args.domain,
                    target=args.target,
                )
            )
            return 0

        if args.command == "recap" and args.recap_command == "today":
            print_json(service.recap_today(domain=args.domain))
            return 0

        if args.command == "recap" and args.recap_command == "approvals":
            print_json(service.recap_approvals(domain=args.domain))
            return 0

        if args.command == "recap" and args.recap_command == "drafts":
            print_json(service.recap_drafts(domain=args.domain))
            return 0

        if args.command == "recap" and args.recap_command == "failures":
            print_json(service.recap_failures(domain=args.domain, limit=args.limit))
            return 0

        if args.command == "recap" and args.recap_command == "external-actions":
            print_json(service.recap_external_actions(domain=args.domain, limit=args.limit))
            return 0

        if args.command == "recap" and args.recap_command == "overdue":
            print_json(service.recap_overdue(domain=args.domain, threshold_hours=args.threshold_hours))
            return 0

        if args.command == "recap" and args.recap_command == "in-progress":
            print_json(service.recap_in_progress(domain=args.domain))
            return 0

        if args.command == "adapter" and args.adapter_command == "execute":
            service.execute_custom_adapter_action(
                adapter_name=args.adapter_name,
                action_name=args.action_name,
            )
            print_json({"status": "ok"})
            return 0

        if args.command == "notion" and args.notion_command == "create-task":
            classification = RequestClassification(
                domain=args.domain,
                intent_type="capture",
                risk_level=args.risk_level,
            ).validate()
            print_json(
                service.create_notion_task(
                    user_request=args.user_request,
                    classification=classification,
                    title=args.title,
                    status=args.status,
                    task_type=args.task_type,
                    area=args.area,
                    target=args.target,
                    request_metadata=parse_metadata_json(args.metadata_json),
                    operation_key=args.operation_key,
                )
            )
            return 0

        if args.command == "notion" and args.notion_command == "query-tasks":
            classification = RequestClassification(
                domain=args.domain,
                intent_type="read",
                risk_level=args.risk_level,
            ).validate()
            print_json(
                service.query_notion_tasks(
                    user_request=args.request,
                    classification=classification,
                    status=args.status,
                    updated_since=args.updated_since,
                    limit=args.limit,
                    request_metadata=parse_metadata_json(args.metadata_json),
                )
            )
            return 0

        if args.command == "notion" and args.notion_command == "sync-tasks":
            classification = RequestClassification(
                domain=args.domain,
                intent_type="read",
                risk_level=args.risk_level,
            ).validate()
            print_json(
                service.sync_notion_tasks(
                    user_request=args.request,
                    classification=classification,
                    statuses=args.status,
                    updated_since=args.updated_since,
                    limit=args.limit,
                    target=args.target,
                    request_metadata=parse_metadata_json(args.metadata_json),
                )
            )
            return 0

        if args.command == "notion" and args.notion_command == "get-task":
            classification = RequestClassification(
                domain=args.domain,
                intent_type="read",
                risk_level=args.risk_level,
            ).validate()
            print_json(
                service.get_notion_task(
                    user_request=args.request,
                    classification=classification,
                    page_id=args.page_id,
                    request_metadata=parse_metadata_json(args.metadata_json),
                )
            )
            return 0

        if args.command == "notion" and args.notion_command == "update-status":
            print_json(
                service.update_notion_task_status(
                    task_id=args.task_id,
                    notion_page_id=args.page_id,
                    backend_status=args.backend_status,
                    note=args.note,
                )
            )
            return 0

        if args.command == "notion" and args.notion_command == "append-note":
            print_json(
                service.append_notion_task_note(
                    task_id=args.task_id,
                    notion_page_id=args.page_id,
                    note=args.note,
                )
            )
            return 0

        if args.command == "daily-routine" and args.daily_routine_command == "run":
            result = service.run_daily_routine(
                payload=build_daily_routine_payload(args),
                create_notion_tasks=not args.no_notion,
                send_email=getattr(args, "send_email", False),
            )
            print_json(result)
            if result.get("email_sent"):
                print(
                    f"DAILY_ROUTINE_COMPLETE\n"
                    f"email_sent_to: {result['recap']['recipient']}\n"
                    f"follow_up_tasks_created: {len(result['created_followups'])}\n"
                    f"recap_date: {result['recap']['run_date']}",
                    file=sys.stderr,
                )
            return 0

        if args.command == "calendar":
            from .calendar_poller import poll_calendar
            from .calendar_writer import (
                add_reminder,
                block_time,
                create_event,
                delete_event,
                update_event,
            )

            if args.calendar_command == "list":
                print_json(poll_calendar())
                return 0

            if args.calendar_command == "create":
                result = create_event(
                    title=args.title,
                    start=args.start,
                    end=args.end,
                    description=args.description,
                    location=args.location,
                    attendees=args.attendees,
                    reminders_minutes=args.reminders,
                    timezone=args.timezone,
                )
                print_json(result)
                return 0

            if args.calendar_command == "block":
                result = block_time(
                    title=args.title,
                    start=args.start,
                    end=args.end,
                    description=args.description,
                    timezone=args.timezone,
                )
                print_json(result)
                return 0

            if args.calendar_command == "update":
                result = update_event(
                    args.event_id,
                    title=args.title,
                    start=args.start,
                    end=args.end,
                    description=args.description,
                    location=args.location,
                    reminders_minutes=args.reminders,
                    timezone=args.timezone,
                )
                print_json(result)
                return 0

            if args.calendar_command == "delete":
                ok = delete_event(args.event_id)
                print_json({"deleted": ok, "event_id": args.event_id})
                return 0 if ok else 2

            if args.calendar_command == "remind":
                result = add_reminder(args.event_id, args.minutes)
                print_json(result)
                return 0

        if args.command == "retry":
            print_json(service.retry_task(args.task_id, feedback=args.feedback))
            return 0

        if args.command == "health":
            from .health import get_system_health
            print_json(get_system_health(service))
            return 0

        if args.command == "stall-check":
            print_json(service.flag_stalled_tasks(threshold_hours=args.threshold_hours))
            return 0
    except (
        KeyError,
        sqlite3.IntegrityError,
        sqlite3.OperationalError,
        NotImplementedError,
        NotionError,
    ) as exc:
        print_json({"error": str(exc), "error_type": type(exc).__name__})
        return 2
    except (OperatorError, ValueError) as exc:
        payload = {"error": str(exc), "error_type": type(exc).__name__}
        if isinstance(exc, OperatorError):
            payload["error_code"] = exc.code
            payload["details"] = exc.details
        print_json(payload)
        return 2

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
