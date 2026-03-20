from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from .artifacts import ArtifactRecord, ArtifactStore
from .adapters import execute_custom_adapter
from .audit import AuditLog
from .config import Paths, load_policy_rules
from .models import ACTION_SOURCES, ApprovalRecord, OperatorError, RequestClassification, TaskRecord, validate_choice
from .storage import Database


class AgenticOSService:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.db = Database(paths.db_path)
        self.audit = AuditLog(paths.audit_log_path)
        self.artifacts = ArtifactStore(paths.artifacts_dir)

    def initialize(self) -> None:
        self.db.initialize()
        self.audit.ensure()
        self.artifacts.ensure()

    def create_request(
        self,
        *,
        user_request: str,
        classification: RequestClassification,
        target: Optional[str] = None,
        request_metadata: Optional[dict[str, Any]] = None,
        external_write: bool = False,
        operation_key: Optional[str] = None,
        artifact_type: Optional[str] = None,
        artifact_content: Optional[Any] = None,
        result_summary: Optional[str] = None,
        external_ref: Optional[str] = None,
        action_source: str = "manual",
    ) -> dict[str, Any]:
        validate_choice(action_source, ACTION_SOURCES, "action_source")
        if operation_key is not None:
            existing_tasks = self.db.list_tasks_by_operation_key(operation_key)
            if existing_tasks:
                existing_task = existing_tasks[0]
                raise ValueError(
                    f"operation_key {operation_key} is already assigned to task {existing_task.id}"
                )
        policy_decision = self.evaluate_policy(
            classification=classification,
            target=target,
            external_write=external_write,
        )
        if policy_decision == "approval_required" and not operation_key:
            raise ValueError("approval_required requests must include an operation_key")
        task_status, approval_state = self._task_state_for_policy(policy_decision)
        task = self.db.create_task(
            classification=RequestClassification(
                domain=classification.domain,
                intent_type=classification.intent_type,
                risk_level=classification.risk_level,
                status=task_status,
                approval_state=approval_state,
            ).validate(),
            user_request=user_request,
            result_summary=result_summary,
            external_ref=external_ref,
            target=target,
            request_metadata_json=self._dump_json_or_none(request_metadata),
            operation_key=operation_key,
            external_write=external_write,
            policy_decision=policy_decision,
            action_source=action_source,
        )
        self._append_event(task_id=task.id, event_type="task_created", payload={"task": asdict(task)})
        self._append_event(task_id=task.id, event_type="task_classified", payload=asdict(classification))
        self._append_event(
            task_id=task.id,
            event_type="policy_evaluated",
            payload={
                "policy_decision": policy_decision,
                "target": target,
                "external_write": external_write,
                "action_source": action_source,
            },
        )

        artifact = None
        if artifact_content is not None:
            artifact = self._create_artifact(
                task_id=task.id,
                artifact_type=artifact_type or "request_context",
                artifact_content=artifact_content,
                event_type="draft_created",
            )
            task = self.db.update_task(task.id, artifact_ref=artifact.id)

        approval = None
        if policy_decision == "approval_required":
            approval = self._create_approval_for_task(task=task, artifact=artifact)

        return {
            "task": task,
            "policy_decision": policy_decision,
            "approval": approval,
        }

    def record_openclaw_read(
        self,
        *,
        user_request: str,
        classification: RequestClassification,
        tool_name: str,
        tool_input: Optional[dict[str, Any]] = None,
        tool_result: Optional[dict[str, Any]] = None,
        summary: str,
        target: Optional[str] = None,
        request_metadata: Optional[dict[str, Any]] = None,
        artifact_type: Optional[str] = None,
        artifact_content: Optional[Any] = None,
        action_source: str = "openclaw_tool",
    ) -> dict[str, Any]:
        payload = self.create_request(
            user_request=user_request,
            classification=classification,
            target=target,
            request_metadata=request_metadata,
            external_write=False,
            artifact_type=artifact_type,
            artifact_content=artifact_content,
            action_source=action_source,
        )
        task = payload["task"]
        operator = self._operator_payload(
            action_source=action_source,
            target=target,
            tool_name=tool_name,
        )
        self._append_event(
            task_id=task.id,
            event_type="tool_called",
            payload={**operator, "tool_input": tool_input or {}},
        )
        self._append_event(
            task_id=task.id,
            event_type="tool_result",
            payload={**operator, "tool_result": tool_result or {}},
        )
        task = self.db.update_task(task.id, result_summary=summary, status="completed")
        self._append_event(
            task_id=task.id,
            event_type="summary_recorded",
            payload={**operator, "result_summary": summary},
        )
        self._append_event(
            task_id=task.id,
            event_type="task_completed",
            payload={"result_summary": summary},
        )
        return {
            "task": task,
            "policy_decision": payload["policy_decision"],
            "approval": payload["approval"],
        }

    def record_openclaw_draft(
        self,
        *,
        user_request: str,
        classification: RequestClassification,
        draft_artifact: Any,
        artifact_type: str,
        tool_name: Optional[str] = None,
        tool_input: Optional[dict[str, Any]] = None,
        summary: Optional[str] = None,
        target: Optional[str] = None,
        request_metadata: Optional[dict[str, Any]] = None,
        action_source: str = "openclaw_skill",
    ) -> dict[str, Any]:
        payload = self.create_request(
            user_request=user_request,
            classification=classification,
            target=target,
            request_metadata=request_metadata,
            artifact_type=artifact_type,
            artifact_content=draft_artifact,
            action_source=action_source,
        )
        task = payload["task"]
        operator = self._operator_payload(
            action_source=action_source,
            target=target,
            tool_name=tool_name,
        )
        if tool_name is not None:
            self._append_event(
                task_id=task.id,
                event_type="tool_called",
                payload={**operator, "tool_input": tool_input or {}},
            )
        artifact = self._artifact_payload_for_task(task.id)
        self._append_event(
            task_id=task.id,
            event_type="draft_generated",
            payload={**operator, "artifact": artifact},
        )
        if summary is not None:
            task = self.db.update_task(task.id, result_summary=summary)
            self._append_event(
                task_id=task.id,
                event_type="summary_recorded",
                payload={**operator, "result_summary": summary},
            )
        return {
            "task": task,
            "policy_decision": payload["policy_decision"],
            "approval": payload["approval"],
        }

    def record_openclaw_execution(
        self,
        *,
        user_request: str,
        classification: RequestClassification,
        tool_name: str,
        operation_key: str,
        result_summary: str,
        target: Optional[str] = None,
        request_metadata: Optional[dict[str, Any]] = None,
        tool_input: Optional[dict[str, Any]] = None,
        tool_result: Optional[dict[str, Any]] = None,
        artifact_type: Optional[str] = None,
        artifact_content: Optional[Any] = None,
        action_source: str = "openclaw_tool",
    ) -> dict[str, Any]:
        payload = self.create_request(
            user_request=user_request,
            classification=classification,
            target=target,
            request_metadata=request_metadata,
            external_write=True,
            operation_key=operation_key,
            artifact_type=artifact_type,
            artifact_content=artifact_content,
            action_source=action_source,
        )
        task = payload["task"]
        operator = self._operator_payload(
            action_source=action_source,
            target=target,
            tool_name=tool_name,
            operation_key=operation_key,
        )
        self._append_event(
            task_id=task.id,
            event_type="action_execution_requested",
            payload={**operator, "tool_input": tool_input or {}},
        )
        return {
            "task": task,
            "policy_decision": payload["policy_decision"],
            "approval": payload["approval"],
            "pending_execution_result": {
                "result_summary": result_summary,
                "tool_result": tool_result or {},
            },
        }

    def trace_task(self, task_id: str) -> dict[str, Any]:
        return self.get_task_detail(task_id)

    def list_tasks(
        self,
        *,
        limit: int = 20,
        status: Optional[str] = None,
        domain: Optional[str] = None,
        target: Optional[str] = None,
        action_source: Optional[str] = None,
    ) -> list[TaskRecord]:
        return self.db.query_tasks(
            limit=limit,
            status=status,
            domain=domain,
            target=target,
            action_source=action_source,
        )

    def list_approvals(self, task_id: Optional[str] = None) -> list[ApprovalRecord]:
        return self.db.list_approvals(task_id=task_id)

    def get_task_detail(self, task_id: str) -> dict[str, Any]:
        task = self.db.get_task(task_id)
        artifacts = self.db.list_artifacts(task_id)
        approvals = [asdict(approval) for approval in self.db.list_approvals(task_id)]
        execution = None
        execution_conflict = None
        operation_key_task_conflicts: list[str] = []
        if task.operation_key:
            task_ids_for_key = [
                record.id
                for record in self.db.list_tasks_by_operation_key(task.operation_key)
                if record.id != task.id
            ]
            operation_key_task_conflicts = sorted(task_ids_for_key)
            execution_record = self.db.get_execution(task.operation_key)
            if execution_record is not None and execution_record.task_id == task.id:
                execution = asdict(execution_record)
            elif execution_record is not None:
                execution_conflict = {
                    "operation_key": task.operation_key,
                    "owner_task_id": execution_record.task_id,
                    "owner_execution_status": execution_record.status,
                }
        events = self._decoded_audit_events(task_id)
        return {
            "task": asdict(task),
            "summary": {
                "artifact_count": len(artifacts),
                "approval_count": len(approvals),
                "audit_event_count": len(events),
                "has_execution": execution is not None,
                "execution_conflict": execution_conflict is not None,
            },
            "artifacts": artifacts,
            "approvals": approvals,
            "execution": execution,
            "execution_conflict": execution_conflict,
            "operation_key_task_conflicts": operation_key_task_conflicts,
            "audit_events": events,
        }

    def get_approval_detail(self, approval_id: str) -> dict[str, Any]:
        approval = self.db.get_approval(approval_id)
        task = self.db.get_task(approval.task_id)
        return {
            "approval": asdict(approval),
            "task": asdict(task),
            "approval_payload": json.loads(approval.payload_json),
            "task_summary": {
                "artifact_ref": task.artifact_ref,
                "operation_key": task.operation_key,
                "target": task.target,
                "action_source": task.action_source,
            },
        }

    def get_execution_detail(self, operation_key: str) -> dict[str, Any]:
        execution = self.db.get_execution(operation_key)
        if execution is None:
            task = self.db.get_task_by_operation_key(operation_key)
            if task is not None:
                return {
                    "operation_key": operation_key,
                    "execution": None,
                    "task": asdict(task),
                    "status": "not_executed",
                    "message": "Operation key exists on a task, but no execution has been recorded yet.",
                }
            raise KeyError(f"unknown operation_key: {operation_key}")
        task = self.db.get_task(execution.task_id)
        approval = self.db.get_approval(execution.approval_id) if execution.approval_id else None
        return {
            "operation_key": operation_key,
            "execution": asdict(execution),
            "task": asdict(task),
            "approval": asdict(approval) if approval else None,
            "audit_events": self._decoded_audit_events(task.id),
        }

    def list_recent_audit_activity(
        self,
        *,
        limit: int = 20,
        domain: Optional[str] = None,
        target: Optional[str] = None,
    ) -> dict[str, Any]:
        events = []
        for event in self.db.list_recent_audit_events(limit=limit, domain=domain, target=target):
            events.append(
                {
                    "id": event["id"],
                    "task_id": event["task_id"],
                    "event_type": event["event_type"],
                    "created_at": event["created_at"],
                    "domain": event["domain"],
                    "task_status": event["task_status"],
                    "target": event["target"],
                    "action_source": event["action_source"],
                    "payload": json.loads(event["payload_json"]),
                }
            )
        return {
            "filters": {"limit": limit, "domain": domain, "target": target},
            "events": events,
        }

    def recap_today(self, *, domain: Optional[str] = None) -> dict[str, Any]:
        tasks = self.list_tasks(limit=500, domain=domain)
        today = self._today_utc_date()
        todays_tasks = [task for task in tasks if task.created_at[:10] == today]
        return {
            "scope": "today",
            "date": today,
            "domain": domain,
            "counts": self._task_counts(todays_tasks),
            "by_domain": self._group_task_counts(todays_tasks, key_name="domain"),
            "items": [self._task_snapshot(task) for task in todays_tasks],
        }

    def recap_approvals(self, *, domain: Optional[str] = None) -> dict[str, Any]:
        approvals = []
        for approval in self.db.list_approvals_by_status("pending"):
            task = self.db.get_task(approval.task_id)
            if domain is not None and task.domain != domain:
                continue
            approvals.append(
                {
                    "approval_id": approval.id,
                    "task_id": task.id,
                    "domain": task.domain,
                    "target": task.target,
                    "action_source": task.action_source,
                    "subject_type": approval.subject_type,
                    "operation_key": approval.operation_key,
                    "created_at": approval.created_at,
                    "user_request": task.user_request,
                }
            )
        return {
            "scope": "awaiting_approval",
            "domain": domain,
            "count": len(approvals),
            "items": approvals,
        }

    def recap_drafts(self, *, domain: Optional[str] = None) -> dict[str, Any]:
        tasks = self.list_tasks(limit=500, status="awaiting_input", domain=domain)
        return {
            "scope": "open_drafts",
            "domain": domain,
            "count": len(tasks),
            "items": [self._task_snapshot(task) for task in tasks],
        }

    def recap_failures(self, *, domain: Optional[str] = None, limit: int = 20) -> dict[str, Any]:
        tasks = self.list_tasks(limit=500, status="failed", domain=domain)
        recent_failures = tasks[:limit]
        return {
            "scope": "recent_failures",
            "domain": domain,
            "count": len(recent_failures),
            "items": [self._task_snapshot(task) for task in recent_failures],
        }

    def recap_external_actions(self, *, domain: Optional[str] = None, limit: int = 20) -> dict[str, Any]:
        tasks = self.list_tasks(limit=500, domain=domain)
        items = [
            {
                "task_id": task.id,
                "domain": task.domain,
                "target": task.target,
                "status": task.status,
                "action_source": task.action_source,
                "operation_key": task.operation_key,
                "result_summary": task.result_summary,
                "created_at": task.created_at,
                "external_write": task.external_write,
            }
            for task in tasks
            if task.external_write or task.intent_type == "execute"
        ]
        return {
            "scope": "external_actions",
            "domain": domain,
            "count": len(items[:limit]),
            "items": items[:limit],
        }

    def revise_artifact(
        self,
        task_id: str,
        *,
        artifact_type: Optional[str],
        artifact_content: Any,
    ) -> dict[str, Any]:
        task = self.db.get_task(task_id)
        artifacts = self.db.list_artifacts(task_id)
        next_version = 1
        if artifacts:
            next_version = max(artifact["version"] for artifact in artifacts) + 1
        artifact = self.artifacts.write(
            task_id=task_id,
            artifact_type=artifact_type or self._default_artifact_type(task, artifacts),
            content=artifact_content,
            version=next_version,
        )
        self.db.insert_artifact(
            artifact_id=artifact.id,
            task_id=task_id,
            artifact_type=artifact.artifact_type,
            path=artifact.path,
            version=artifact.version,
            content_preview=artifact.content_preview,
            created_at=artifact.created_at,
        )
        task = self.db.update_task(task_id, artifact_ref=artifact.id)
        self._append_event(
            task_id=task.id,
            event_type="artifact_updated",
            payload={"artifact": self.artifacts.to_payload(artifact)},
        )

        approval = None
        pending_approval = self.db.get_pending_approval_for_task(task_id)
        if task.policy_decision == "approval_required":
            if pending_approval is not None:
                cancelled = self.db.update_approval(
                    pending_approval.id,
                    status="cancelled",
                    decision_note="Superseded by revised artifact version.",
                )
                self._append_event(
                    task_id=task.id,
                    event_type="approval_cancelled",
                    payload={"approval": asdict(cancelled)},
                )
            task = self.db.update_task(task.id, status="awaiting_approval", approval_state="pending")
            approval = self._create_approval_for_task(task=task, artifact=artifact)

        return {
            "task": task,
            "artifact": artifact,
            "approval": approval,
        }

    def approve(self, approval_id: str, decision_note: Optional[str] = None) -> dict[str, Any]:
        approval = self.db.get_approval(approval_id)
        if approval.status != "pending":
            self._record_task_operation_rejection(
                task_id=approval.task_id,
                code="approval_not_pending",
                message=f"approval {approval_id} is not pending",
                operation="approval.approve",
                approval_id=approval_id,
                current_status=approval.status,
            )
        approval = self.db.update_approval(approval_id, status="approved", decision_note=decision_note)
        task = self.db.update_task(approval.task_id, status="approved", approval_state="approved")
        self._append_event(task_id=task.id, event_type="approval_granted", payload={"approval": asdict(approval)})
        return {"task": task, "approval": approval}

    def deny(self, approval_id: str, decision_note: Optional[str] = None) -> dict[str, Any]:
        approval = self.db.get_approval(approval_id)
        if approval.status != "pending":
            self._record_task_operation_rejection(
                task_id=approval.task_id,
                code="approval_not_pending",
                message=f"approval {approval_id} is not pending",
                operation="approval.deny",
                approval_id=approval_id,
                current_status=approval.status,
            )
        approval = self.db.update_approval(approval_id, status="denied", decision_note=decision_note)
        task = self.db.update_task(
            approval.task_id,
            status="cancelled",
            approval_state="denied",
            result_summary=decision_note or "Approval denied.",
        )
        self._append_event(task_id=task.id, event_type="approval_denied", payload={"approval": asdict(approval)})
        self._append_event(
            task_id=task.id,
            event_type="task_cancelled",
            payload={"reason": task.result_summary or "Approval denied."},
        )
        return {"task": task, "approval": approval}

    def cancel(self, approval_id: str, decision_note: Optional[str] = None) -> dict[str, Any]:
        approval = self.db.get_approval(approval_id)
        if approval.status != "pending":
            self._record_task_operation_rejection(
                task_id=approval.task_id,
                code="approval_not_pending",
                message=f"approval {approval_id} is not pending",
                operation="approval.cancel",
                approval_id=approval_id,
                current_status=approval.status,
            )
        approval = self.db.update_approval(approval_id, status="cancelled", decision_note=decision_note)
        task = self.db.update_task(
            approval.task_id,
            status="cancelled",
            approval_state="cancelled",
            result_summary=decision_note or "Approval cancelled.",
        )
        self._append_event(task_id=task.id, event_type="approval_cancelled", payload={"approval": asdict(approval)})
        self._append_event(
            task_id=task.id,
            event_type="task_cancelled",
            payload={"reason": task.result_summary or "Approval cancelled."},
        )
        return {"task": task, "approval": approval}

    def execute_action(
        self,
        task_id: str,
        result_summary: str,
        *,
        tool_name: Optional[str] = None,
        tool_result: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        task = self.db.get_task(task_id)
        if not task.operation_key:
            self._record_task_operation_rejection(
                task_id=task_id,
                code="missing_operation_key",
                message=f"task {task_id} has no operation_key",
                operation="task.execute",
            )
        if task.policy_decision == "approval_required" and task.approval_state != "approved":
            self._record_task_operation_rejection(
                task_id=task_id,
                code="approval_required",
                message=f"task {task_id} is not approved for execution",
                operation="task.execute",
                current_approval_state=task.approval_state,
            )
        conflicting_tasks = [
            record.id
            for record in self.db.list_tasks_by_operation_key(task.operation_key)
            if record.id != task.id
        ]
        if conflicting_tasks:
            conflict_payload = {
                "operation_key": task.operation_key,
                "reason": "operation_key_assigned_to_multiple_tasks",
                "conflicting_task_ids": sorted(conflicting_tasks),
            }
            self._append_event(
                task_id=task.id,
                event_type="action_execution_rejected",
                payload=conflict_payload,
            )
            return {
                "task": task,
                "execution": None,
                "duplicate": True,
                "duplicate_reason": "operation_key_assigned_to_multiple_tasks",
                "conflicting_task_ids": sorted(conflicting_tasks),
            }

        existing = self.db.get_execution(task.operation_key)
        if existing is not None:
            duplicate_reason = (
                "already_executed_for_task"
                if existing.task_id == task.id
                else "operation_key_owned_by_other_task"
            )
            self._append_event(
                task_id=task.id,
                event_type="action_execution_rejected",
                payload={
                    "operation_key": task.operation_key,
                    "reason": duplicate_reason,
                    "existing_execution": asdict(existing),
                },
            )
            return {
                "task": task,
                "execution": existing,
                "duplicate": True,
                "duplicate_reason": duplicate_reason,
                "conflicting_task_ids": [existing.task_id] if existing.task_id != task.id else [],
            }

        latest_approval = None
        approvals = self.db.list_approvals(task_id)
        if approvals:
            latest_approval = approvals[0]
        execution = self.db.create_execution(
            operation_key=task.operation_key,
            task_id=task.id,
            approval_id=latest_approval.id if latest_approval and latest_approval.status == "approved" else None,
            status="executed",
            result_summary=result_summary,
        )
        task = self.db.update_task(task.id, status="executed", result_summary=result_summary)
        operator = self._operator_payload(
            action_source=task.action_source,
            target=task.target,
            tool_name=tool_name,
            operation_key=task.operation_key,
        )
        self._append_event(
            task_id=task.id,
            event_type="action_execution_recorded",
            payload={
                **operator,
                "status": execution.status,
                "result_summary": result_summary,
                "tool_result": tool_result or {},
            },
        )
        self._append_event(
            task_id=task.id,
            event_type="action_executed",
            payload={
                **operator,
                "status": execution.status,
                "result_summary": result_summary,
            },
        )
        task = self.db.update_task(task.id, status="completed", result_summary=result_summary)
        self._append_event(
            task_id=task.id,
            event_type="task_completed",
            payload={"result_summary": result_summary},
        )
        return {
            "task": task,
            "execution": execution,
            "duplicate": False,
            "duplicate_reason": None,
            "conflicting_task_ids": [],
        }

    def execute_custom_adapter_action(self, *, adapter_name: str, action_name: str) -> None:
        execute_custom_adapter(adapter_name=adapter_name, action_name=action_name)

    def complete_task(self, task_id: str, result_summary: str) -> TaskRecord:
        task = self.db.get_task(task_id)
        if task.status in {"completed", "cancelled"}:
            self._record_task_operation_rejection(
                task_id=task_id,
                code="task_not_completable",
                message=f"task {task_id} cannot be completed from status {task.status}",
                operation="task.complete",
                current_status=task.status,
            )
        task = self.db.update_task(task_id, status="completed", result_summary=result_summary)
        self._append_event(
            task_id=task_id,
            event_type="task_completed",
            payload={"result_summary": result_summary},
        )
        return task

    def fail_task(self, task_id: str, reason: str) -> TaskRecord:
        task = self.db.get_task(task_id)
        if task.status in {"completed", "cancelled"}:
            self._record_task_operation_rejection(
                task_id=task_id,
                code="task_not_failable",
                message=f"task {task_id} cannot be failed from status {task.status}",
                operation="task.fail",
                current_status=task.status,
            )
        task = self.db.update_task(task_id, status="failed", result_summary=reason)
        self._append_event(task_id=task.id, event_type="task_failed", payload={"reason": reason})
        return task

    def evaluate_policy(
        self,
        *,
        classification: RequestClassification,
        target: Optional[str],
        external_write: bool,
    ) -> str:
        rules = load_policy_rules(self.paths.policy_rules_path)
        context = {
            "domain": classification.domain,
            "intent_type": classification.intent_type,
            "risk_level": classification.risk_level,
            "target": target,
            "external_write": external_write,
        }
        for rule in rules:
            match = rule.get("match", {})
            if all(context.get(key) == value for key, value in match.items()):
                action = rule["action"]
                if action not in ("read_ok", "draft_required", "approval_required"):
                    raise ValueError(f"unsupported policy action: {action}")
                return action
        raise ValueError(f"no policy rule matched for {context}")

    def _create_artifact(
        self,
        *,
        task_id: str,
        artifact_type: str,
        artifact_content: Any,
        event_type: str,
    ) -> ArtifactRecord:
        artifact = self.artifacts.write(
            task_id=task_id,
            artifact_type=artifact_type,
            content=artifact_content,
            version=1,
        )
        self.db.insert_artifact(
            artifact_id=artifact.id,
            task_id=task_id,
            artifact_type=artifact.artifact_type,
            path=artifact.path,
            version=artifact.version,
            content_preview=artifact.content_preview,
            created_at=artifact.created_at,
        )
        self._append_event(
            task_id=task_id,
            event_type=event_type,
            payload={"artifact": self.artifacts.to_payload(artifact)},
        )
        return artifact

    def _create_approval_for_task(
        self,
        *,
        task: TaskRecord,
        artifact: Optional[ArtifactRecord],
    ) -> ApprovalRecord:
        approval_payload = {
            "task_id": task.id,
            "user_request": task.user_request,
            "target": task.target,
            "operation_key": task.operation_key,
            "external_write": task.external_write,
            "policy_decision": task.policy_decision,
            "request_metadata": self._load_json_or_none(task.request_metadata_json),
        }
        subject_type = "action"
        artifact_id = None
        if artifact is not None:
            subject_type = "artifact"
            artifact_id = artifact.id
            approval_payload["artifact"] = self.artifacts.to_payload(artifact)

        approval = self.db.create_approval(
            approval_id=f"apr_{uuid4().hex[:12]}",
            task_id=task.id,
            status="pending",
            subject_type=subject_type,
            artifact_id=artifact_id,
            action_target=task.target,
            operation_key=task.operation_key,
            payload_json=json.dumps(approval_payload, sort_keys=True),
        )
        self._append_event(task_id=task.id, event_type="approval_requested", payload={"approval": asdict(approval)})
        return approval

    @staticmethod
    def _task_state_for_policy(policy_decision: str) -> tuple[str, str]:
        if policy_decision == "approval_required":
            return ("awaiting_approval", "pending")
        if policy_decision == "draft_required":
            return ("awaiting_input", "not_needed")
        return ("in_progress", "not_needed")

    @staticmethod
    def _dump_json_or_none(payload: Optional[dict[str, Any]]) -> Optional[str]:
        if payload is None:
            return None
        return json.dumps(payload, sort_keys=True)

    @staticmethod
    def _load_json_or_none(payload: Optional[str]) -> Optional[dict[str, Any]]:
        if payload is None:
            return None
        return json.loads(payload)

    @staticmethod
    def _default_artifact_type(task: TaskRecord, artifacts: list[dict[str, Any]]) -> str:
        if artifacts:
            return str(artifacts[-1]["artifact_type"])
        if task.intent_type == "draft":
            return "draft"
        return "request_context"

    def _artifact_payload_for_task(self, task_id: str) -> Optional[dict[str, Any]]:
        artifacts = self.db.list_artifacts(task_id)
        if not artifacts:
            return None
        return artifacts[-1]

    @staticmethod
    def _operator_payload(
        *,
        action_source: str,
        target: Optional[str],
        tool_name: Optional[str] = None,
        operation_key: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"action_source": action_source}
        if target is not None:
            payload["target"] = target
        if tool_name is not None:
            payload["tool_name"] = tool_name
        if operation_key is not None:
            payload["operation_key"] = operation_key
        return payload

    def _append_event(self, *, task_id: str, event_type: str, payload: dict[str, Any]) -> None:
        event_id = self.db.insert_audit_event(
            task_id=task_id,
            event_type=event_type,
            payload_json=json.dumps(payload, sort_keys=True),
        )
        self.audit.append(
            task_id=task_id,
            event_type=event_type,
            payload=payload,
            event_id=event_id,
        )

    def _decoded_audit_events(self, task_id: str) -> list[dict[str, Any]]:
        return [
            {
                "id": event["id"],
                "task_id": event["task_id"],
                "event_type": event["event_type"],
                "created_at": event["created_at"],
                "payload": json.loads(event["payload_json"]),
            }
            for event in self.db.list_audit_events(task_id)
        ]

    def _record_task_operation_rejection(
        self,
        *,
        task_id: str,
        code: str,
        message: str,
        operation: str,
        **details: Any,
    ) -> None:
        self._append_event(
            task_id=task_id,
            event_type="operation_rejected",
            payload={"code": code, "message": message, "operation": operation, **details},
        )
        raise OperatorError(code=code, message=message, details={"task_id": task_id, "operation": operation, **details})

    @staticmethod
    def _task_snapshot(task: TaskRecord) -> dict[str, Any]:
        return {
            "task_id": task.id,
            "domain": task.domain,
            "intent_type": task.intent_type,
            "status": task.status,
            "approval_state": task.approval_state,
            "target": task.target,
            "action_source": task.action_source,
            "operation_key": task.operation_key,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "result_summary": task.result_summary,
            "user_request": task.user_request,
        }

    @staticmethod
    def _task_counts(tasks: list[TaskRecord]) -> dict[str, int]:
        counts: dict[str, int] = {"total": len(tasks)}
        for task in tasks:
            counts[task.status] = counts.get(task.status, 0) + 1
        return counts

    @staticmethod
    def _group_task_counts(tasks: list[TaskRecord], *, key_name: str) -> dict[str, dict[str, int]]:
        groups: dict[str, dict[str, int]] = {}
        for task in tasks:
            key = getattr(task, key_name) or "unknown"
            group = groups.setdefault(key, {"total": 0})
            group["total"] += 1
            group[task.status] = group.get(task.status, 0) + 1
        return groups

    @staticmethod
    def _today_utc_date() -> str:
        return datetime.now(timezone.utc).date().isoformat()
