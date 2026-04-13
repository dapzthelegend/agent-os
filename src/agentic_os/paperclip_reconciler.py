from __future__ import annotations

import logging
from typing import Any

from .config import AppConfig, Paths
from .service import AgenticOSService
from .status_mapping import map_paperclip_status_to_backend
from .task_control_plane import TaskControlPlane

log = logging.getLogger(__name__)


class PaperclipReconciler:
    """One-way lifecycle mirror: Paperclip issue status -> backend task status."""

    def __init__(self, paths: Paths, config: AppConfig) -> None:
        self._paths = paths
        self._config = config
        self._service = AgenticOSService(paths, config)
        self._cp: TaskControlPlane | None = None
        if config.paperclip is not None:
            self._cp = TaskControlPlane(config.paperclip)

    def run_once(self) -> dict[str, Any]:
        if self._cp is None:
            return {
                "issues_scanned": 0,
                "imported": 0,
                "mirrored": 0,
                "unchanged": 0,
                "errors": 0,
                "note": "paperclip not configured",
            }

        imported = 0
        mirrored = 0
        promoted = 0
        unchanged = 0
        errors = 0

        try:
            issues = self._cp.list_all_issues(limit=500)
        except Exception as exc:  # noqa: BLE001
            log.error("reconciler: list_all_issues failed: %s", exc)
            return {
                "issues_scanned": 0,
                "imported": 0,
                "mirrored": 0,
                "promoted": 0,
                "unchanged": 0,
                "errors": 1,
            }

        for issue in issues:
            try:
                task = self._service.db.get_task_by_paperclip_issue_id(issue.id)
                if task is None:
                    routine_origin = self._derive_origin_kind(issue)
                    created = self._service.import_paperclip_issue(
                        issue_id=issue.id,
                        title=issue.title,
                        description=issue.description,
                        paperclip_status=issue.status,
                        project_id=issue.project_id,
                        routine_id=getattr(issue, "routine_id", None),
                        routine_run_id=getattr(issue, "routine_run_id", None),
                        origin_kind=routine_origin,
                        assignee_agent_id=getattr(issue, "assignee_id", None),
                        labels=getattr(issue, "labels", None) or [],
                    )
                    task = created["task"]
                    imported += 1

                target_status = map_paperclip_status_to_backend(issue.status)
                if task.status == target_status:
                    unchanged += 1
                else:
                    self._service.db.update_task(task.id, status=target_status)
                    self._service._append_event(
                        task_id=task.id,
                        event_type="paperclip_status_mirrored",
                        payload={
                            "paperclip_issue_id": issue.id,
                            "paperclip_status": issue.status,
                            "backend_status": target_status,
                        },
                    )
                    mirrored += 1

                if issue.status == "backlog" and self._is_ready_for_paperclip_todo(task):
                    updated_issue = self._cp.promote_issue_to_todo(issue.id, task)
                    if updated_issue is not None:
                        self._service.db.update_task(
                            task.id,
                            paperclip_assignee_agent_id=updated_issue.assignee_id or None,
                            paperclip_project_id=updated_issue.project_id or None,
                        )
                        self._service._append_event(
                            task_id=task.id,
                            event_type="reconciler_action_taken",
                            payload={
                                "paperclip_issue_id": issue.id,
                                "action": "promote_backlog_to_todo",
                                "policy_decision": task.policy_decision,
                                "approval_state": task.approval_state,
                            },
                        )
                        promoted += 1
            except Exception as exc:  # noqa: BLE001
                errors += 1
                log.error("reconciler: issue mirror failed for %s: %s", issue.id, exc)

        return {
            "issues_scanned": len(issues),
            "imported": imported,
            "mirrored": mirrored,
            "promoted": promoted,
            "unchanged": unchanged,
            "errors": errors,
        }

    @staticmethod
    def _is_ready_for_paperclip_todo(task: Any) -> bool:
        if getattr(task, "status", None) == "done":
            return False
        policy = getattr(task, "policy_decision", None)
        if policy in ("approve", "approve_plan", "approval_required"):
            return getattr(task, "approval_state", None) == "approved"
        return True

    @staticmethod
    def _derive_origin_kind(issue: Any) -> str:
        origin_kind = getattr(issue, "origin_kind", None)
        if isinstance(origin_kind, str) and origin_kind.strip():
            return origin_kind.strip()
        source = getattr(issue, "source", None) or ""
        if isinstance(source, str) and source.startswith("routine."):
            return "routine_execution"
        routine_run_id = getattr(issue, "routine_run_id", None)
        if isinstance(routine_run_id, str) and routine_run_id.strip():
            return "routine_execution"
        routine_id = getattr(issue, "routine_id", None)
        if isinstance(routine_id, str) and routine_id.strip():
            return "routine_execution"
        return "manual_issue"

    def repair_missing_projections(self) -> dict[str, Any]:
        """Legacy hook kept for scheduler compatibility; now just runs mirror sync."""
        return self.run_once()

    def detect_projection_drift(self, sample_limit: int = 50) -> dict[str, Any]:
        """Legacy hook kept for scheduler compatibility; no Paperclip status repair."""
        result = self.run_once()
        return {
            "checked": min(sample_limit, result["issues_scanned"]),
            "drifted": result["mirrored"],
            "repairs_attempted": 0,
            "repairs_succeeded": 0,
            "errors": result["errors"],
        }
