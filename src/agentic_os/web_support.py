from __future__ import annotations

import json
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from .config import default_paths
from .models import ACTION_SOURCES, APPROVAL_RECORD_STATES, DOMAINS, STATUSES, TaskRecord
from .service import AgenticOSService


PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"
IMPORTANT_AUDIT_EVENTS = {
    "policy_evaluated",
    "draft_generated",
    "summary_recorded",
    "action_execution_requested",
    "action_execution_recorded",
    "operation_rejected",
    "approval_denied",
    "approval_cancelled",
    "action_execution_rejected",
    "task_failed",
}


@lru_cache(maxsize=1)
def get_service() -> AgenticOSService:
    service = AgenticOSService(default_paths())
    try:
        service.initialize()
    except PermissionError:
        paths = service.paths
        if not (paths.db_path.exists() and paths.audit_log_path.exists() and paths.artifacts_dir.exists()):
            raise
    return service


def serialize_task(task: TaskRecord) -> dict[str, Any]:
    return asdict(task)


def load_artifact_content(service: AgenticOSService, artifact: dict[str, Any]) -> str:
    return service.artifacts.read_text(str(artifact["path"]))


def parse_artifact_input(artifact_text: str, artifact_json: str) -> Any:
    if artifact_text and artifact_json:
        raise ValueError("provide artifact_text or artifact_json, not both")
    if artifact_json:
        return json.loads(artifact_json)
    if artifact_text:
        return artifact_text
    raise ValueError("artifact_text or artifact_json is required")


def format_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def build_overview(service: AgenticOSService) -> dict[str, Any]:
    tasks = service.list_tasks(limit=500)
    status_counts: dict[str, int] = {status: 0 for status in STATUSES}
    for task in tasks:
        status_counts[task.status] = status_counts.get(task.status, 0) + 1

    pending_approvals = service.recap_approvals()
    awaiting_input = service.recap_awaiting_input()
    failures = service.recap_failures(limit=5)
    external_actions = service.recap_external_actions(limit=5)

    return {
        "task_count": len(tasks),
        "status_counts": status_counts,
        "pending_approvals_count": pending_approvals["count"],
        "awaiting_input_count": awaiting_input["count"],
        "recent_failures_count": failures["count"],
        "recent_external_actions_count": external_actions["count"],
        "recent_tasks": [serialize_task(task) for task in tasks[:10]],
        "pending_approvals": pending_approvals.get("records", [])[:5],
        "recent_failures": failures.get("records", []),
        "recent_external_actions": external_actions.get("records", []),
    }


def task_filter_options(service: AgenticOSService) -> dict[str, list[str]]:
    tasks = service.list_tasks(limit=500)
    targets = sorted({task.target for task in tasks if task.target})
    return {
        "statuses": list(STATUSES),
        "domains": list(DOMAINS),
        "targets": targets,
        "action_sources": list(ACTION_SOURCES),
    }


def enrich_task_detail(service: AgenticOSService, task_id: str, artifact_id: Optional[str]) -> dict[str, Any]:
    detail = service.get_task_detail(task_id)
    selected_artifact = None
    selected_artifact_content = None

    for artifact in detail["artifacts"]:
        if artifact_id is None or artifact["id"] == artifact_id:
            selected_artifact = artifact
    if selected_artifact is None and detail["artifacts"]:
        selected_artifact = detail["artifacts"][-1]
    if selected_artifact is not None:
        selected_artifact_content = load_artifact_content(service, selected_artifact)

    detail["request_metadata"] = (
        json.loads(detail["task"]["request_metadata_json"])
        if detail["task"]["request_metadata_json"]
        else None
    )
    detail["selected_artifact"] = selected_artifact
    detail["selected_artifact_content"] = selected_artifact_content
    return detail


def enrich_approval_detail(service: AgenticOSService, approval_id: str) -> dict[str, Any]:
    detail = service.get_approval_detail(approval_id)
    artifact_id = detail["approval"].get("artifact_id")
    artifact_content = None
    if artifact_id:
        task_detail = service.get_task_detail(detail["task"]["id"])
        for artifact in task_detail["artifacts"]:
            if artifact["id"] == artifact_id:
                artifact_content = load_artifact_content(service, artifact)
                break
    detail["artifact_content"] = artifact_content
    return detail


def approval_groups(service: AgenticOSService) -> dict[str, list[dict[str, Any]]]:
    groups = {status: [] for status in APPROVAL_RECORD_STATES}
    for approval in service.list_approvals():
        groups.setdefault(approval.status, []).append(asdict(approval))
    return groups


def annotate_audit_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    annotated = []
    for event in events:
        enriched = dict(event)
        enriched["is_important"] = event["event_type"] in IMPORTANT_AUDIT_EVENTS
        enriched["payload_pretty"] = format_json(event["payload"])
        annotated.append(enriched)
    return annotated
