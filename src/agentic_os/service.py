from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from .artifacts import ArtifactRecord, ArtifactStore
from .adapters import execute_custom_adapter
from .audit import AuditLog
from .config import AppConfig, Paths, load_app_config, load_policy_rules
from .daily_routine import (
    DailyRoutineInput,
    FollowUpAction,
    YesterdayRecap,
    build_daily_recap,
    extract_follow_up_actions,
    infer_domain,
    prepare_email_payload,
    render_html_email,
    render_plaintext_email,
    summarize_task_for_yesterday,
)
from .models import ACTION_SOURCES, ApprovalRecord, OperatorError, RequestClassification, TaskRecord, validate_choice
from .notion import NotionAdapter, NotionTask
from .storage import Database


class AgenticOSService:
    def __init__(self, paths: Paths, config: Optional[AppConfig] = None) -> None:
        self.paths = paths
        self.config = config or load_app_config(paths)
        self.db = Database(paths.db_path)
        self.audit = AuditLog(paths.audit_log_path)
        self.artifacts = ArtifactStore(paths.artifacts_dir)
        self._notion_adapter: Optional[NotionAdapter] = None
        self._cp_cache: Any = None
        self._cp_initialized: bool = False

    @property
    def _cp(self) -> Any:
        """Lazy TaskControlPlane — None when Paperclip is not configured."""
        if not self._cp_initialized:
            if self.config.paperclip is not None:
                from .task_control_plane import TaskControlPlane
                self._cp_cache = TaskControlPlane(self.config.paperclip)
            self._cp_initialized = True
        return self._cp_cache

    def initialize(self) -> None:
        self.db.initialize()
        self.audit.ensure()
        self.artifacts.ensure()
        # 7.5 — Config validation: log issues to stderr; send Discord DM on failure
        from .health import validate_startup_config
        issues = validate_startup_config(self.paths, self.config)
        if issues:
            import sys
            for issue in issues:
                print(f"[agentic-os] CONFIG WARNING: {issue}", file=sys.stderr)
            try:
                from .notification_router import _send_with_fallback
                msg = "agentic-os startup config issues:\n" + "\n".join(f"• {i}" for i in issues)
                _send_with_fallback(msg, channel_hint="startup_config")
            except Exception:
                pass

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
        adopt_paperclip_issue_id: Optional[str] = None,
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
            action_source=action_source,
        )

        from .plan_gate import classify_task_mode
        task_mode = classify_task_mode(
            classification.domain, classification.intent_type, classification.risk_level
        )

        # plan_first tasks bypass the approval_required operation_key requirement —
        # the plan gate is the gating mechanism instead
        if policy_decision == "approval_required" and not operation_key and task_mode != "plan_first":
            raise ValueError("approval_required requests must include an operation_key")

        if task_mode == "plan_first":
            # Plan gate supersedes the policy-based status: task starts in planning
            task_status = "planning"
            approval_state = "not_needed"
        else:
            task_status, approval_state = self._task_state_for_policy(policy_decision)

        # Ensure the intake classifier's agent selection is stored in request_metadata
        # so that resolve_executor_key() can route codex tasks to executor_codex.
        effective_metadata: dict[str, Any] = dict(request_metadata) if request_metadata else {}
        if "agent" not in effective_metadata:
            try:
                from .dispatcher import Dispatcher as _Dispatcher
                _dispatcher = _Dispatcher()
                _, resolved_agent = _dispatcher.resolve_routing(classification)
                effective_metadata["agent"] = resolved_agent
            except Exception:
                pass

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
            request_metadata_json=self._dump_json_or_none(effective_metadata) if effective_metadata else None,
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

        # Persist task_mode (plan_first or direct)
        if task_mode != "direct":
            task = self.db.update_task(task.id, task_mode=task_mode)
            self._append_event(
                task_id=task.id,
                event_type="task_mode_set",
                payload={"task_mode": task_mode},
            )

        # Paperclip projection (phase 1) — failure must never block task creation
        cp = self._cp
        if cp is not None:
            if adopt_paperclip_issue_id:
                # Adopt an existing Paperclip issue (task was created there manually)
                try:
                    assignee_key = cp.resolve_assignee_for_task(task)
                    issue = cp.adopt_issue(
                        adopt_paperclip_issue_id,
                        task,
                        assignee_key=assignee_key,
                    )
                    task = self.db.update_task(
                        task.id,
                        paperclip_issue_id=adopt_paperclip_issue_id,
                        paperclip_project_id=(issue.project_id if issue else None),
                        paperclip_goal_id=(issue.goal_id if issue else None),
                        paperclip_assignee_agent_id=(issue.assignee_id if issue else None),
                    )
                    self._append_event(
                        task_id=task.id,
                        event_type="paperclip_issue_imported",
                        payload={"issue_id": adopt_paperclip_issue_id, "assignee_key": assignee_key},
                    )
                except Exception as _pc_exc:
                    self._append_event(
                        task_id=task.id,
                        event_type="paperclip_sync_failed",
                        payload={"error": str(_pc_exc), "phase": "adopt"},
                    )
            else:
                try:
                    assignee_key = cp.resolve_assignee_for_task(task)
                    issue = cp.create_issue(task, assignee_key=assignee_key)
                    if issue and issue.id:
                        task = self.db.update_task(
                            task.id,
                            paperclip_issue_id=issue.id,
                            paperclip_project_id=issue.project_id or None,
                            paperclip_goal_id=issue.goal_id or None,
                            paperclip_assignee_agent_id=issue.assignee_id or None,
                        )
                        self._append_event(
                            task_id=task.id,
                            event_type="paperclip_issue_created",
                            payload={"issue_id": issue.id, "assignee_key": assignee_key},
                        )
                    else:
                        self._append_event(
                            task_id=task.id,
                            event_type="paperclip_projection_failed",
                            payload={"error": "create_issue returned None — check server logs"},
                        )
                except Exception as _pc_exc:
                    self._append_event(
                        task_id=task.id,
                        event_type="paperclip_projection_failed",
                        payload={"error": str(_pc_exc)},
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
        if policy_decision == "approval_required" and task_mode != "plan_first":
            approval = self._create_approval_for_task(task=task, artifact=artifact)

        return {
            "task": task,
            "policy_decision": policy_decision,
            "approval": approval,
            "task_mode": task_mode,
        }

    def import_paperclip_issue(
        self,
        *,
        issue_id: str,
        title: str,
        description: str,
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Import a manually-created Paperclip issue as an agentic-os task.

        Called by the reconciler when it detects a new issue with no matching
        backend task.  Infers domain from project_id, classifies with
        sensible defaults, then calls create_request() with
        adopt_paperclip_issue_id so the existing issue is adopted rather
        than a new one created.

        Returns the same dict as create_request(): {task, policy_decision, ...}
        """
        # Infer domain from project_id
        domain = "system"  # safe default
        if project_id and self.config.paperclip is not None:
            reverse_map = {v: k for k, v in self.config.paperclip.project_map.items()}
            domain = reverse_map.get(project_id, "system")

        user_request = title
        if description and description.strip() and description.strip() != title.strip():
            user_request = f"{title}\n\n{description}"

        classification = RequestClassification(
            domain=domain,
            intent_type="execute",
            risk_level="medium",
            status="new",
            approval_state="not_needed",
        )

        return self.create_request(
            user_request=user_request,
            classification=classification,
            action_source="paperclip_manual",
            adopt_paperclip_issue_id=issue_id,
        )

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

    # ── Execution loop helpers ────────────────────────────────────────────────

    def list_ready_tasks(self, *, limit: int = 20) -> list[TaskRecord]:
        """Tasks eligible for execution: approved, or new with read_ok policy."""
        return self.db.query_ready_tasks(limit=limit)

    def pickup_task(self, task_id: str, *, claimed_by: str = "task_executor_cron") -> dict:
        """Atomically claim a task for execution. Emits task_picked_up audit event."""
        result = self.db.pickup_task(task_id, claimed_by=claimed_by)
        if result["success"]:
            self._append_event(
                task_id=task_id,
                event_type="task_picked_up",
                payload={"task_id": task_id, "claimed_by": claimed_by},
            )
            cp = self._cp
            if cp is not None:
                try:
                    task = self.db.get_task(task_id)
                    if task.paperclip_issue_id:
                        # plan_first tasks move to 'executing'; direct tasks to 'in_progress'
                        backend_status = "executing" if task.task_mode == "plan_first" else "in_progress"
                        cp.update_issue_status(task.paperclip_issue_id, backend_status)
                except Exception as _pc_exc:
                    self._append_event(
                        task_id=task_id,
                        event_type="paperclip_sync_failed",
                        payload={"error": str(_pc_exc), "op": "pickup"},
                    )
        return result

    def mark_dispatched(self, task_id: str, *, session_key: str, agent: str) -> None:
        """Record that a child session was spawned. Emits task_dispatched audit event."""
        self.db.update_dispatch_session_key(task_id, session_key)
        self._append_event(
            task_id=task_id,
            event_type="task_dispatched",
            payload={"task_id": task_id, "session_key": session_key, "agent": agent},
        )

    def record_spawn_failure(self, task_id: str, *, reason: str) -> TaskRecord:
        """Mark a task failed after a spawn error. Emits spawn_failed audit event."""
        task = self.fail_task(task_id, reason=f"spawn_failed: {reason}")
        self._append_event(
            task_id=task_id,
            event_type="spawn_failed",
            payload={"task_id": task_id, "reason": reason},
        )
        return task

    def requeue_task(self, task_id: str) -> TaskRecord:
        """
        Reset a stalled task back to its pre-execution ready state.
        Returns to 'approved' if it was explicitly approved, else 'new'.
        Emits task_requeued audit event.
        """
        task = self.db.get_task(task_id)
        if task.status not in ("stalled", "failed", "in_progress"):
            raise ValueError(f"task {task_id} is not requeue-eligible (status={task.status})")
        target_status = "approved" if task.approval_state == "approved" else "new"
        self.db.update_task(task_id, status=target_status)
        self._append_event(
            task_id=task_id,
            event_type="task_requeued",
            payload={"task_id": task_id, "from_status": task.status, "to_status": target_status},
        )
        return self.db.get_task(task_id)

    # ── Plan gate (phase 2) ───────────────────────────────────────────────────

    def submit_plan(
        self,
        task_id: str,
        plan_text: str,
        *,
        version: Optional[int] = None,
    ) -> TaskRecord:
        """
        Submit a plan for an operator review.

        Transitions: planning → awaiting_plan_review
        Writes plan to local artifact store + Paperclip document.
        Paperclip issue moves to in_review, assigned to chief_of_staff.
        """
        task = self.db.get_task(task_id)
        if task.task_mode != "plan_first":
            raise ValueError(f"task {task_id} is not in plan_first mode (mode={task.task_mode})")
        if task.status != "planning":
            raise ValueError(
                f"task {task_id} must be in 'planning' status to submit a plan (status={task.status})"
            )

        plan_version = version if version is not None else (task.plan_version or 0) + 1

        # Store plan as a local artifact
        artifact = self._create_artifact(
            task_id=task_id,
            artifact_type="plan_document",
            artifact_content={"plan_text": plan_text, "version": plan_version},
            event_type="plan_submitted",
        )

        # Advance task status
        task = self.db.update_task(
            task_id,
            status="awaiting_plan_review",
            plan_version=plan_version,
        )
        self._append_event(
            task_id=task_id,
            event_type="plan_awaiting_review",
            payload={"plan_version": plan_version, "artifact_id": artifact.id},
        )

        # Paperclip: write plan doc, move to in_review, assign to project_manager for review.
        # The PM is the plan reviewer — not the author.  CoS is not involved at this step.
        cp = self._cp
        if cp is not None:
            try:
                if task.paperclip_issue_id:
                    cp.write_plan_doc(task.paperclip_issue_id, plan_text, version=plan_version)
                    cp.update_issue_status(
                        task.paperclip_issue_id,
                        "awaiting_plan_review",
                        assignee_key="project_manager",
                    )
            except Exception as _pc_exc:
                self._append_event(
                    task_id=task_id,
                    event_type="paperclip_sync_failed",
                    payload={"error": str(_pc_exc), "op": "submit_plan"},
                )

        return task

    def approve_plan(
        self,
        task_id: str,
        *,
        revision_id: str,
    ) -> TaskRecord:
        """
        Approve a submitted plan.

        Transitions: awaiting_plan_review → approved_for_execution
        Stores approved_plan_revision_id — execution is now unblocked.
        Paperclip: status → todo, assignee → engineer.
        """
        task = self.db.get_task(task_id)
        if task.task_mode != "plan_first":
            raise ValueError(f"task {task_id} is not in plan_first mode")
        if task.status != "awaiting_plan_review":
            raise ValueError(
                f"task {task_id} must be in 'awaiting_plan_review' to approve (status={task.status})"
            )

        task = self.db.update_task(
            task_id,
            status="approved_for_execution",
            approved_plan_revision_id=revision_id,
        )
        self._append_event(
            task_id=task_id,
            event_type="plan_approved",
            payload={"revision_id": revision_id},
        )

        cp = self._cp
        if cp is not None:
            try:
                if task.paperclip_issue_id:
                    # Reassign to the execution agent who wrote the plan — they now execute it.
                    executor_key = cp.resolve_executor_key(task)
                    cp.update_issue_status(
                        task.paperclip_issue_id,
                        "approved_for_execution",
                        assignee_key=executor_key,
                    )
                    cp.add_comment(
                        task.paperclip_issue_id,
                        f"Plan approved (revision {revision_id}). Ready for execution.",
                    )
            except Exception as _pc_exc:
                self._append_event(
                    task_id=task_id,
                    event_type="paperclip_sync_failed",
                    payload={"error": str(_pc_exc), "op": "approve_plan"},
                )

        return task

    def reject_plan(
        self,
        task_id: str,
        *,
        feedback: str,
    ) -> TaskRecord:
        """
        Reject a submitted plan and send it back for revision.

        Transitions: awaiting_plan_review → planning
        Paperclip: comment with feedback, status back to in_progress.
        """
        task = self.db.get_task(task_id)
        if task.task_mode != "plan_first":
            raise ValueError(f"task {task_id} is not in plan_first mode")
        if task.status != "awaiting_plan_review":
            raise ValueError(
                f"task {task_id} must be in 'awaiting_plan_review' to reject (status={task.status})"
            )

        task = self.db.update_task(task_id, status="planning")
        self._append_event(
            task_id=task_id,
            event_type="plan_rejected",
            payload={"feedback": feedback},
        )

        cp = self._cp
        if cp is not None:
            try:
                if task.paperclip_issue_id:
                    cp.update_issue_status(task.paperclip_issue_id, "planning")
                    cp.add_comment(
                        task.paperclip_issue_id,
                        f"REVISE: {feedback}",
                    )
            except Exception as _pc_exc:
                self._append_event(
                    task_id=task_id,
                    event_type="paperclip_sync_failed",
                    payload={"error": str(_pc_exc), "op": "reject_plan"},
                )

        return task

    def cancel_task(self, task_id: str, *, reason: str = "Cancelled by operator") -> TaskRecord:
        """
        Cancel a task directly by task_id (no approval record required).

        Used by the Paperclip reconciler when an operator sets a task to
        'cancelled' in Paperclip.  Does NOT write back to Paperclip — the
        signal originated there, so we only update backend state.

        No-ops if task is already in a terminal state.
        """
        task = self.db.get_task(task_id)
        if task.status in {"cancelled", "completed", "failed"}:
            return task
        task = self.db.update_task(task_id, status="cancelled", result_summary=reason)
        self._append_event(
            task_id=task_id,
            event_type="task_cancelled",
            payload={"reason": reason, "source": "paperclip_reconciler"},
        )
        return task

    def set_task_mode(self, task_id: str, *, mode: str) -> TaskRecord:
        """
        Set the task_mode field for a task.

        Valid modes: 'plan_first', 'direct'.
        Intended for operator use via the dashboard or API to override
        the mode assigned at creation time.
        """
        if mode not in {"plan_first", "direct"}:
            raise ValueError(f"invalid task_mode: {mode!r} — must be 'plan_first' or 'direct'")
        self.db.get_task(task_id)  # raises KeyError if not found
        task = self.db.update_task(task_id, task_mode=mode)
        self._append_event(
            task_id=task_id,
            event_type="task_mode_set",
            payload={"task_mode": mode, "source": "operator"},
        )
        return task

    # ─────────────────────────────────────────────────────────────────────────

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

        # Pending approvals count (all time, not just today)
        pending_approvals = self.db.list_approvals_by_status("pending")
        pending_approval_count = sum(
            1 for a in pending_approvals
            if domain is None or self.db.get_task(a.task_id).domain == domain
        )

        # In-progress tasks (all time)
        in_progress_tasks = [
            task for task in self.list_tasks(limit=500, domain=domain)
            if task.status in {"in_progress", "awaiting_input", "awaiting_approval"}
        ]

        # Overdue tasks (>48h no update, not terminal)
        overdue = self._find_overdue_tasks(domain=domain)

        return {
            "scope": "today",
            "date": today,
            "domain": domain,
            "counts": self._task_counts(todays_tasks),
            "by_domain": self._group_task_counts(todays_tasks, key_name="domain"),
            "pending_approvals_count": pending_approval_count,
            "in_progress_count": len(in_progress_tasks),
            "overdue_count": len(overdue),
            "records": [self._task_snapshot(task) for task in todays_tasks],
            "in_progress": [self._task_snapshot(task) for task in in_progress_tasks[:10]],
            "overdue": overdue[:10],
        }

    def recap_approvals(self, *, domain: Optional[str] = None) -> dict[str, Any]:
        approvals = []
        now_utc = datetime.now(timezone.utc)
        for approval in self.db.list_approvals_by_status("pending"):
            task = self.db.get_task(approval.task_id)
            if domain is not None and task.domain != domain:
                continue
            hours_pending = self._hours_since(approval.created_at, now_utc)
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
                    "hours_pending": round(hours_pending, 1),
                    "escalation_flag": hours_pending >= 2.0,
                }
            )
        # Sort by longest-pending first
        approvals.sort(key=lambda r: r["hours_pending"], reverse=True)
        return {
            "scope": "awaiting_approval",
            "domain": domain,
            "count": len(approvals),
            "escalated_count": sum(1 for r in approvals if r["escalation_flag"]),
            "records": approvals,
        }

    def recap_overdue(self, *, domain: Optional[str] = None, threshold_hours: float = 48.0) -> dict[str, Any]:
        """Return tasks that have had no update for more than threshold_hours and are not in a terminal state."""
        overdue = self._find_overdue_tasks(domain=domain, threshold_hours=threshold_hours)
        return {
            "scope": "overdue",
            "domain": domain,
            "threshold_hours": threshold_hours,
            "count": len(overdue),
            "records": overdue,
        }

    def recap_in_progress(self, *, domain: Optional[str] = None) -> dict[str, Any]:
        """Return all tasks currently in a non-terminal, non-completed state."""
        tasks = [
            task for task in self.list_tasks(limit=500, domain=domain)
            if task.status in {"in_progress", "awaiting_input", "awaiting_approval", "approved"}
        ]
        return {
            "scope": "in_progress",
            "domain": domain,
            "count": len(tasks),
            "records": [self._task_snapshot(task) for task in tasks],
        }

    def recap_drafts(self, *, domain: Optional[str] = None) -> dict[str, Any]:
        tasks = self.list_tasks(limit=500, status="awaiting_input", domain=domain)
        return {
            "scope": "open_drafts",
            "domain": domain,
            "count": len(tasks),
            "records": [self._task_snapshot(task) for task in tasks],
        }

    def recap_failures(self, *, domain: Optional[str] = None, limit: int = 20) -> dict[str, Any]:
        tasks = self.list_tasks(limit=500, status="failed", domain=domain)
        recent_failures = tasks[:limit]
        return {
            "scope": "recent_failures",
            "domain": domain,
            "count": len(recent_failures),
            "records": [self._task_snapshot(task) for task in recent_failures],
        }

    def recap_external_actions(self, *, domain: Optional[str] = None, limit: int = 20) -> dict[str, Any]:
        tasks = self.list_tasks(limit=500, domain=domain)
        records = [
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
            "count": len(records[:limit]),
            "records": records[:limit],
        }

    def run_daily_routine(
        self,
        *,
        payload: dict[str, Any],
        create_notion_tasks: bool = True,
        send_email: bool = False,
    ) -> dict[str, Any]:
        routine_input = DailyRoutineInput.from_dict(payload)
        classification = RequestClassification(
            domain="system",
            intent_type="recap",
            risk_level="low",
        ).validate()
        request_payload = self.create_request(
            user_request=f"Generate daily routine recap for {routine_input.date}",
            classification=classification,
            target="daily_routine",
            request_metadata={
                "date": routine_input.date,
                "timezone": routine_input.timezone,
                "recipient": routine_input.recipient,
                "delivery_time": routine_input.delivery_time,
            },
            action_source="custom_adapter",
        )
        task = request_payload["task"]

        yesterday = self._build_yesterday_recap(routine_input.date)
        # Enrich recap with live pending approvals and overdue tasks (Phase 6 gap closed)
        pending_approvals_raw = [
            {"approval_id": a.id, "task_id": a.task_id, "subject_type": a.subject_type,
             "hours_pending": self._hours_since(a.created_at)}
            for a in self.db.list_approvals(status="pending")
        ]
        overdue_tasks_raw = self._find_overdue_tasks(domain=None, threshold_hours=48.0)
        recap = build_daily_recap(
            routine_input,
            yesterday,
            pending_approvals=pending_approvals_raw,
            overdue_tasks=overdue_tasks_raw,
        )
        email_body = render_plaintext_email(recap)
        email_payload = prepare_email_payload(recap, email_body)
        artifact = self._create_artifact(
            task_id=task.id,
            artifact_type="daily_routine_recap",
            artifact_content={
                "input": payload,
                "recap": recap.to_dict(),
                "email_payload": email_payload,
            },
            event_type="daily_routine_recap_created",
        )
        task = self.db.update_task(
            task.id,
            artifact_ref=artifact.id,
            result_summary=f"Generated daily recap for {routine_input.date}.",
            status="completed",
        )
        self._append_event(
            task_id=task.id,
            event_type="daily_routine_email_prepared",
            payload=email_payload,
        )

        created_followups = []
        created_notion_tasks = []
        skipped_followups = []
        for action in extract_follow_up_actions(routine_input):
            existing = self.db.get_task_by_operation_key(action.operation_key)
            if existing is not None:
                skipped_followups.append(
                    {
                        "operation_key": action.operation_key,
                        "reason": f"existing task {existing.id}",
                    }
                )
                continue
            followup = self._create_daily_followup_task(action=action, parent_task_id=task.id)
            created_followups.append({"task": asdict(followup), "action": self._serialize_follow_up_action(action)})
            if create_notion_tasks and action.notion_title is not None:
                notion_result = self._create_daily_followup_notion_task(
                    action=action,
                    parent_task_id=task.id,
                    backend_task_id=followup.id,
                )
                if notion_result is not None:
                    created_notion_tasks.append(notion_result)

        summary = (
            f"Generated daily recap for {routine_input.date} with "
            f"{len(created_followups)} follow-up task(s)."
        )
        task = self.db.update_task(task.id, result_summary=summary, status="completed")
        self._append_event(
            task_id=task.id,
            event_type="daily_routine_followups_created",
            payload={
                "follow_up_count": len(created_followups),
                "notion_task_count": len(created_notion_tasks),
                "skipped_follow_up_count": len(skipped_followups),
            },
        )
        self._append_event(
            task_id=task.id,
            event_type="task_completed",
            payload={"result_summary": summary},
        )
        email_sent = False
        email_send_error: Optional[str] = None
        if send_email:
            from .gmail_sender import send_email as _send_gmail
            html_body = render_html_email(recap)
            ok = _send_gmail(
                to=routine_input.recipient,
                subject=email_payload["subject"],
                body=email_body,
                html_body=html_body,
            )
            email_sent = ok
            if not ok:
                email_send_error = "gmail_send_failed"
            self._append_event(
                task_id=task.id,
                event_type="daily_routine_email_sent" if ok else "daily_routine_email_send_failed",
                payload={
                    "to": routine_input.recipient,
                    "subject": email_payload["subject"],
                    "html": html_body is not None,
                },
            )

        return {
            "task": task,
            "recap": recap.to_dict(),
            "email_payload": email_payload,
            "email_body": email_body,
            "email_sent": email_sent,
            "email_send_error": email_send_error,
            "created_followups": created_followups,
            "created_notion_tasks": created_notion_tasks,
            "skipped_followups": skipped_followups,
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

    def send_approval_reminders(self, threshold_hours: float = 1.0) -> dict[str, Any]:
        """Send reminders for all pending approvals older than threshold_hours."""
        from .notification_router import route_approval_reminder
        now_utc = datetime.now(timezone.utc)
        results = []
        for approval in self.db.list_approvals_by_status("pending"):
            task = self.db.get_task(approval.task_id)
            hours = self._hours_since(approval.created_at, now_utc)
            if hours >= threshold_hours:
                result = route_approval_reminder(task, approval, hours)
                results.append({
                    "approval_id": approval.id,
                    "task_id": task.id,
                    "hours_pending": round(hours, 1),
                    "channel": result.channel,
                    "sent": result.success,
                })
        return {
            "reminded": len(results),
            "threshold_hours": threshold_hours,
            "records": results,
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
        self._try_send_approved_draft(task, approval)
        return {"task": task, "approval": approval}

    def _try_send_approved_draft(self, task: TaskRecord, approval: ApprovalRecord) -> None:
        """If the approved task is a gmail_reply_draft, send the draft email to the original sender."""
        if task.target != "gmail_reply_draft":
            return
        try:
            artifacts = self.db.list_artifacts(task.id)
            draft_artifact = next(
                (a for a in artifacts if a["artifact_type"] == "email_draft"), None
            )
            if draft_artifact is None:
                self._append_event(
                    task_id=task.id,
                    event_type="draft_send_skipped",
                    payload={"reason": "no email_draft artifact found"},
                )
                return
            content_raw = self.artifacts.read_text(draft_artifact["path"])
            # Handle both plain text and JSON {"body": "..."} format
            try:
                parsed = json.loads(content_raw)
                body = parsed.get("body") or parsed.get("text") or content_raw
            except (json.JSONDecodeError, AttributeError):
                body = content_raw

            metadata: dict[str, Any] = {}
            if task.request_metadata_json:
                try:
                    metadata = json.loads(task.request_metadata_json) or {}
                except json.JSONDecodeError:
                    pass
            sender = str(metadata.get("sender") or "")
            subject = str(metadata.get("subject") or task.user_request.splitlines()[0])
            if not sender:
                self._append_event(
                    task_id=task.id,
                    event_type="draft_send_skipped",
                    payload={"reason": "no sender in request_metadata_json"},
                )
                return
            from .gmail_sender import send_email
            ok = send_email(to=sender, subject=f"Re: {subject}", body=str(body))
            self._append_event(
                task_id=task.id,
                event_type="draft_sent" if ok else "draft_send_failed",
                payload={"to": sender, "subject": subject, "success": ok},
            )
        except Exception as exc:  # noqa: BLE001
            import sys
            print(f"[service] _try_send_approved_draft failed: {exc}", file=sys.stderr)
            self._append_event(
                task_id=task.id,
                event_type="draft_send_failed",
                payload={"error": str(exc)},
            )

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
        cp = self._cp
        if cp is not None:
            try:
                if task.paperclip_issue_id:
                    cp.update_issue_status(task.paperclip_issue_id, "cancelled")
                    cp.add_comment(task.paperclip_issue_id, f"Cancelled: {task.result_summary or 'Approval denied.'}")
            except Exception as _pc_exc:
                self._append_event(
                    task_id=task.id,
                    event_type="paperclip_sync_failed",
                    payload={"error": str(_pc_exc), "op": "deny"},
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
        cp = self._cp
        if cp is not None:
            try:
                if task.paperclip_issue_id:
                    cp.update_issue_status(task.paperclip_issue_id, "cancelled")
                    cp.add_comment(task.paperclip_issue_id, f"Cancelled: {task.result_summary or 'Approval cancelled.'}")
            except Exception as _pc_exc:
                self._append_event(
                    task_id=task.id,
                    event_type="paperclip_sync_failed",
                    payload={"error": str(_pc_exc), "op": "cancel"},
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
        approval_id_for_exec = latest_approval.id if latest_approval and latest_approval.status == "approved" else None
        execution = self.db.create_execution(
            operation_key=task.operation_key,
            task_id=task.id,
            approval_id=approval_id_for_exec,
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
        correlation = {
            "task_id": task.id,
            "execution_id": execution.operation_key,
            "approval_id": approval_id_for_exec,
        }
        self._append_event(
            task_id=task.id,
            event_type="action_execution_recorded",
            payload={
                **operator,
                **correlation,
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
                **correlation,
                "status": execution.status,
                "result_summary": result_summary,
            },
        )
        task = self.db.update_task(task.id, status="completed", result_summary=result_summary)
        self._append_event(
            task_id=task.id,
            event_type="task_completed",
            payload={**correlation, "result_summary": result_summary},
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

    def create_notion_task(
        self,
        *,
        user_request: str,
        classification: RequestClassification,
        title: str,
        status: Optional[str] = None,
        task_type: Optional[str] = None,
        area: Optional[str] = None,
        target: str = "notion_task",
        request_metadata: Optional[dict[str, Any]] = None,
        operation_key: Optional[str] = None,
    ) -> dict[str, Any]:
        payload = self.create_request(
            user_request=user_request,
            classification=classification,
            target=target,
            request_metadata=request_metadata,
            external_write=True,
            operation_key=operation_key,
            action_source="custom_adapter",
        )
        task = payload["task"]
        adapter = self._require_notion_adapter()
        adapter_payload = {
            "adapter_name": "notion",
            "action_name": "create_task",
            "database_id": self.config.notion.database_id if self.config.notion else None,
            "title": title,
            "status": status,
            "task_type": task_type,
            "area": area or classification.domain,
            "backend_task_id": task.id,
            "operation_key": operation_key,
        }
        self._append_event(task_id=task.id, event_type="adapter_called", payload=adapter_payload)
        try:
            notion_task = adapter.create_task(
                title=title,
                status=status,
                task_type=task_type,
                area=area or classification.domain,
                backend_task_id=task.id,
                operation_key=operation_key,
                last_agent_update=f"Created by agentic-os task {task.id}",
            )
        except Exception as exc:
            return self._fail_adapter_task(task, adapter_payload, exc)
        task = self.db.update_task(
            task.id,
            external_ref=notion_task.page_id,
            result_summary=f"Created Notion task {notion_task.page_id}.",
            status="completed",
            external_write=True,
        )
        self._append_event(
            task_id=task.id,
            event_type="adapter_result",
            payload={**adapter_payload, "page": self._serialize_notion_task(notion_task)},
        )
        self._append_event(
            task_id=task.id,
            event_type="task_completed",
            payload={"result_summary": task.result_summary},
        )
        return {"task": task, "notion_task": self._serialize_notion_task(notion_task)}

    def query_notion_tasks(
        self,
        *,
        user_request: str,
        classification: RequestClassification,
        status: Optional[str] = None,
        updated_since: Optional[str] = None,
        limit: int = 20,
        target: str = "notion_task_query",
        request_metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        payload = self.create_request(
            user_request=user_request,
            classification=classification,
            target=target,
            request_metadata=request_metadata,
            external_write=False,
            action_source="custom_adapter",
        )
        task = payload["task"]
        adapter = self._require_notion_adapter()
        adapter_payload = {
            "adapter_name": "notion",
            "action_name": "query_tasks",
            "status": status,
            "updated_since": updated_since,
            "limit": limit,
        }
        self._append_event(task_id=task.id, event_type="adapter_called", payload=adapter_payload)
        try:
            notion_tasks = adapter.query_tasks(status=status, updated_since=updated_since, limit=limit)
        except Exception as exc:
            return self._fail_adapter_task(task, adapter_payload, exc)
        summary = f"Queried {len(notion_tasks)} Notion task(s)."
        task = self.db.update_task(task.id, result_summary=summary, status="completed")
        self._append_event(
            task_id=task.id,
            event_type="adapter_result",
            payload={
                **adapter_payload,
                "items": [self._serialize_notion_task(item) for item in notion_tasks],
            },
        )
        self._append_event(task_id=task.id, event_type="task_completed", payload={"result_summary": summary})
        return {
            "task": task,
            "items": [self._serialize_notion_task(item) for item in notion_tasks],
        }

    def sync_notion_tasks(
        self,
        *,
        user_request: str,
        classification: RequestClassification,
        statuses: Optional[list[str]] = None,
        updated_since: Optional[str] = None,
        limit: int = 50,
        target: str = "notion_task_sync",
        request_metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        status_filters = self._resolve_notion_sync_statuses(statuses)
        payload = self.create_request(
            user_request=user_request,
            classification=classification,
            target=target,
            request_metadata=request_metadata,
            external_write=False,
            action_source="custom_adapter",
        )
        task = payload["task"]
        adapter = self._require_notion_adapter()
        adapter_payload = {
            "adapter_name": "notion",
            "action_name": "sync_tasks",
            "statuses": status_filters,
            "updated_since": updated_since,
            "limit_per_status": limit,
        }
        self._append_event(task_id=task.id, event_type="adapter_called", payload=adapter_payload)
        try:
            notion_by_page_id: dict[str, NotionTask] = {}
            for status in status_filters:
                for notion_task in adapter.query_tasks(
                    status=status,
                    updated_since=updated_since,
                    limit=limit,
                ):
                    notion_by_page_id[notion_task.page_id] = notion_task
        except Exception as exc:
            return self._fail_adapter_task(task, adapter_payload, exc)

        imported: list[dict[str, Any]] = []
        existing: list[dict[str, Any]] = []
        for notion_task in notion_by_page_id.values():
            existing_task = self.db.get_task_by_external_ref(notion_task.page_id)
            if existing_task is not None:
                existing.append(
                    {
                        "task": asdict(existing_task),
                        "notion_task": self._serialize_notion_task(notion_task),
                        "match": "external_ref",
                    }
                )
                continue

            if notion_task.operation_key:
                operation_key_task = self.db.get_task_by_operation_key(notion_task.operation_key)
                if operation_key_task is not None:
                    if operation_key_task.external_ref != notion_task.page_id:
                        operation_key_task = self.db.update_task(
                            operation_key_task.id,
                            external_ref=notion_task.page_id,
                        )
                    existing.append(
                        {
                            "task": asdict(operation_key_task),
                            "notion_task": self._serialize_notion_task(notion_task),
                            "match": "operation_key",
                        }
                    )
                    continue

            synced_task = self._create_synced_notion_capture_task(
                notion_task=notion_task,
                parent_task_id=task.id,
            )
            imported.append(
                {
                    "task": asdict(synced_task),
                    "notion_task": self._serialize_notion_task(notion_task),
                }
            )

        summary = (
            f"Synced {len(notion_by_page_id)} Notion task(s): "
            f"{len(imported)} imported, {len(existing)} already linked."
        )
        task = self.db.update_task(task.id, result_summary=summary, status="completed")
        self._append_event(
            task_id=task.id,
            event_type="adapter_result",
            payload={
                **adapter_payload,
                "queried_count": len(notion_by_page_id),
                "imported_count": len(imported),
                "existing_count": len(existing),
                "imported_task_ids": [item["task"]["id"] for item in imported],
                "existing_task_ids": [item["task"]["id"] for item in existing],
            },
        )
        self._append_event(task_id=task.id, event_type="task_completed", payload={"result_summary": summary})
        return {
            "task": task,
            "statuses": status_filters,
            "updated_since": updated_since,
            "limit_per_status": limit,
            "queried_count": len(notion_by_page_id),
            "imported_count": len(imported),
            "existing_count": len(existing),
            "imported": imported,
            "existing": existing,
        }

    def get_notion_task(
        self,
        *,
        user_request: str,
        classification: RequestClassification,
        page_id: str,
        target: str = "notion_task_detail",
        request_metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        payload = self.create_request(
            user_request=user_request,
            classification=classification,
            target=target,
            request_metadata=request_metadata,
            external_write=False,
            action_source="custom_adapter",
        )
        task = payload["task"]
        adapter = self._require_notion_adapter()
        adapter_payload = {
            "adapter_name": "notion",
            "action_name": "get_task",
            "page_id": page_id,
        }
        self._append_event(task_id=task.id, event_type="adapter_called", payload=adapter_payload)
        try:
            notion_task = adapter.get_task(page_id)
        except Exception as exc:
            return self._fail_adapter_task(task, adapter_payload, exc)
        summary = f"Fetched Notion task {page_id}."
        task = self.db.update_task(task.id, result_summary=summary, status="completed", external_ref=page_id)
        self._append_event(
            task_id=task.id,
            event_type="adapter_result",
            payload={**adapter_payload, "page": self._serialize_notion_task(notion_task)},
        )
        self._append_event(task_id=task.id, event_type="task_completed", payload={"result_summary": summary})
        return {"task": task, "notion_task": self._serialize_notion_task(notion_task)}

    def update_notion_task_status(
        self,
        *,
        task_id: str,
        notion_page_id: Optional[str] = None,
        backend_status: str,
        note: Optional[str] = None,
        target: str = "notion_task_status",
    ) -> dict[str, Any]:
        task = self.db.get_task(task_id)
        adapter = self._require_notion_adapter()
        page_id = notion_page_id or task.external_ref
        if not page_id:
            raise ValueError(f"task {task_id} has no Notion external_ref and no page id was provided")
        notion_status = self._map_backend_status_to_notion(backend_status)
        adapter_payload = {
            "adapter_name": "notion",
            "action_name": "update_task_status",
            "page_id": page_id,
            "backend_status": backend_status,
            "notion_status": notion_status,
        }
        self._append_event(task_id=task.id, event_type="adapter_called", payload=adapter_payload)
        try:
            notion_task = adapter.update_task_status(
                page_id=page_id,
                status=notion_status,
                last_agent_update=note or f"Backend task {task.id} moved to {backend_status}.",
            )
        except Exception as exc:
            return self._fail_adapter_task(task, adapter_payload, exc)
        self._append_event(
            task_id=task.id,
            event_type="adapter_result",
            payload={**adapter_payload, "page": self._serialize_notion_task(notion_task)},
        )
        return {"task": task, "notion_task": self._serialize_notion_task(notion_task)}

    def append_notion_task_note(
        self,
        *,
        task_id: str,
        note: str,
        notion_page_id: Optional[str] = None,
        target: str = "notion_task_note",
    ) -> dict[str, Any]:
        task = self.db.get_task(task_id)
        adapter = self._require_notion_adapter()
        page_id = notion_page_id or task.external_ref
        if not page_id:
            raise ValueError(f"task {task_id} has no Notion external_ref and no page id was provided")
        adapter_payload = {
            "adapter_name": "notion",
            "action_name": "append_note",
            "page_id": page_id,
            "target": target,
        }
        self._append_event(task_id=task.id, event_type="adapter_called", payload=adapter_payload)
        try:
            append_result = adapter.append_note(page_id=page_id, note=note)
            notion_task = adapter.update_task_properties(
                page_id=page_id,
                last_agent_update=note[:500],
            )
        except Exception as exc:
            return self._fail_adapter_task(task, adapter_payload, exc)
        self._append_event(
            task_id=task.id,
            event_type="adapter_result",
            payload={
                **adapter_payload,
                "append_result": append_result,
                "page": self._serialize_notion_task(notion_task),
            },
        )
        return {
            "task": task,
            "append_result": append_result,
            "notion_task": self._serialize_notion_task(notion_task),
        }

    def complete_task(
        self,
        task_id: str,
        result_summary: str = "",
        *,
        artifact_ref: Optional[str] = None,
        paperclip_content: Optional[str] = None,
        artifact_path: Optional[str] = None,
    ) -> TaskRecord:
        """
        Mark task as executed with result summary and optional artifact reference.

        paperclip_content: full result text for Paperclip writeback (uses long-doc path
            when len > LONG_RESULT_THRESHOLD). Defaults to result_summary when not provided.
        artifact_path: filesystem path to the result artifact for Paperclip attachment upload.
        """
        task = self.db.get_task(task_id)
        if task.status in {"completed", "cancelled"}:
            self._record_task_operation_rejection(
                task_id=task_id,
                code="task_not_completable",
                message=f"task {task_id} cannot be completed from status {task.status}",
                operation="task.complete",
                current_status=task.status,
            )
        # Use "executed" for artifact-based completion, "completed" for direct completion
        status = "executed" if artifact_ref else "completed"
        task = self.db.update_task(
            task_id,
            status=status,
            result_summary=result_summary,
            artifact_ref=artifact_ref,
        )
        self._append_event(
            task_id=task_id,
            event_type="task_completed",
            payload={"result_summary": result_summary, "artifact_ref": artifact_ref},
        )

        # Paperclip writeback — status + rich result (doc/comment/artifact)
        cp = self._cp
        if cp is not None:
            try:
                if task.paperclip_issue_id:
                    executor_key = cp.resolve_executor_key(task)
                    cp.update_issue_status(
                        task.paperclip_issue_id,
                        task.status,
                        assignee_key=executor_key,
                    )
                    pc_text = paperclip_content or result_summary
                    if pc_text:
                        from pathlib import Path as _Path
                        cp.write_result(
                            task.paperclip_issue_id,
                            pc_text,
                            task_id=task_id,
                            artifact_path=_Path(artifact_path) if artifact_path else None,
                        )
            except Exception as _pc_exc:
                self._append_event(
                    task_id=task_id,
                    event_type="paperclip_sync_failed",
                    payload={"error": str(_pc_exc), "op": "complete"},
                )

        # Send completion notification
        try:
            from .notifier import notify_task_completed
            word_count = len(result_summary.split()) if task.intent_type == "content" else None
            notify_task_completed(task, word_count=word_count)
        except Exception:
            pass

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

        # Paperclip writeback — status + failure comment
        cp = self._cp
        if cp is not None:
            try:
                if task.paperclip_issue_id:
                    cp.update_issue_status(task.paperclip_issue_id, "failed")
                    cp.post_failure_comment(task.paperclip_issue_id, reason[:500])
            except Exception as _pc_exc:
                self._append_event(
                    task_id=task_id,
                    event_type="paperclip_sync_failed",
                    payload={"error": str(_pc_exc), "op": "fail"},
                )

        # Send Discord notification
        try:
            from .notifier import notify_task_failed
            notify_task_failed(task)
        except Exception:
            pass

        return task

    def reset_task_for_retry(self, task_id: str, *, feedback: str) -> TaskRecord:
        """Reset task to in_progress for retry (max 2 retries)."""
        task = self.db.get_task(task_id)
        retry_count = (task.retry_count or 0) + 1
        
        if retry_count > 2:
            # Max retries exceeded, fail the task
            task = self.db.update_task(
                task_id,
                status="failed",
                result_summary=f"Max retries exceeded. Last feedback: {feedback}",
                retry_count=retry_count,
            )
            self._append_event(
                task_id=task_id,
                event_type="task_failed",
                payload={"reason": "max_retries_exceeded", "feedback": feedback},
            )
        else:
            # Reset to in_progress for retry
            task = self.db.update_task(
                task_id,
                status="in_progress",
                result_summary=f"Retry {retry_count}: {feedback}",
                retry_count=retry_count,
            )
            self._append_event(
                task_id=task_id,
                event_type="task_retry_reset",
                payload={"retry_count": retry_count, "feedback": feedback},
            )
        
        return task

    # ------------------------------------------------------------------
    # 7.1 — Task timeout & recovery
    # ------------------------------------------------------------------

    def flag_stalled_tasks(self, threshold_hours: float = 2.0) -> dict:
        """
        Scan for in_progress/awaiting_input tasks silent for >threshold_hours,
        flag them as stalled, and fire Discord DM alerts.

        Called by the stall-check cron job.
        """
        from .recovery import scan_and_flag_stalled_tasks
        return scan_and_flag_stalled_tasks(self, threshold_hours=threshold_hours)

    def retry_task(self, task_id: str, *, feedback: str = "operator retry") -> dict:
        """
        Operator-initiated retry for a stalled or failed task.

        Delegates to recovery.retry_stalled_task() which calls
        reset_task_for_retry() (max 2 retries enforced there).
        """
        from .recovery import retry_stalled_task
        return retry_stalled_task(self, task_id, feedback=feedback)

    def evaluate_policy(
        self,
        *,
        classification: RequestClassification,
        target: Optional[str],
        external_write: bool,
        action_source: Optional[str] = None,
    ) -> str:
        rules = load_policy_rules(self.paths.policy_rules_path)
        context = {
            "domain": classification.domain,
            "intent_type": classification.intent_type,
            "risk_level": classification.risk_level,
            "target": target,
            "external_write": external_write,
            "action_source": action_source,
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

        # Send smart notification for new approval request
        try:
            from .notifier import notify_approval_requested
            notify_approval_requested(task, approval)
        except Exception:
            pass

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

    def _build_yesterday_recap(self, run_date: str) -> YesterdayRecap:
        tasks = self.list_tasks(limit=500)
        yesterday = self._date_offset(run_date, days=-1)
        completed = [
            summarize_task_for_yesterday(task.user_request, task.result_summary)
            for task in tasks
            if task.updated_at[:10] == yesterday and task.status == "completed"
        ][:5]
        blocked = [
            summarize_task_for_yesterday(task.user_request, task.result_summary)
            for task in tasks
            if task.updated_at[:10] == yesterday and task.status in {"failed", "awaiting_input", "awaiting_approval"}
        ][:5]
        still_open = [
            summarize_task_for_yesterday(task.user_request, task.result_summary)
            for task in tasks
            if task.status in {"new", "in_progress", "awaiting_input", "awaiting_approval", "approved", "executed"}
            and task.created_at[:10] <= yesterday
        ][:5]
        return YesterdayRecap(completed=completed, blocked=blocked, still_open=still_open)

    @staticmethod
    def _date_offset(run_date: str, *, days: int) -> str:
        return (datetime.fromisoformat(run_date).date()).fromordinal(
            datetime.fromisoformat(run_date).date().toordinal() + days
        ).isoformat()

    def _create_daily_followup_task(self, *, action: FollowUpAction, parent_task_id: str) -> TaskRecord:
        payload = self.create_request(
            user_request=action.title,
            classification=RequestClassification(
                domain=action.domain,
                intent_type="capture",
                risk_level="low",
            ).validate(),
            target="daily_routine_followup",
            request_metadata={
                "source": "daily_routine",
                "parent_task_id": parent_task_id,
                "source_kind": action.source_kind,
                "source_title": action.source_title,
                "rationale": action.rationale,
            },
            external_write=False,
            operation_key=action.operation_key,
            result_summary=action.summary,
            action_source="custom_adapter",
        )
        task = self.db.update_task(payload["task"].id, status="completed", result_summary=action.summary)
        self._append_event(
            task_id=task.id,
            event_type="daily_routine_followup_created",
            payload=self._serialize_follow_up_action(action),
        )
        self._append_event(
            task_id=task.id,
            event_type="task_completed",
            payload={"result_summary": action.summary},
        )
        return task

    def _create_daily_followup_notion_task(
        self,
        *,
        action: FollowUpAction,
        parent_task_id: str,
        backend_task_id: str,
    ) -> Optional[dict[str, Any]]:
        if self.config.notion is None:
            return None
        notion_operation_key = f"{action.operation_key}-notion"
        existing = self.db.get_task_by_operation_key(notion_operation_key)
        if existing is not None:
            return {
                "task": asdict(existing),
                "skipped": True,
                "reason": "existing Notion task operation_key",
            }
        try:
            result = self.create_notion_task(
                user_request=f"Create Notion follow-up for {action.title}",
                classification=RequestClassification(
                    domain=infer_domain(action.source_kind, area=action.domain),
                    intent_type="capture",
                    risk_level="low",
                ).validate(),
                title=action.notion_title or action.title,
                status="Inbox",
                task_type="task",
                area=action.domain,
                target="notion_task",
                request_metadata={
                    "source": "daily_routine",
                    "parent_task_id": parent_task_id,
                    "backend_followup_task_id": backend_task_id,
                    "source_kind": action.source_kind,
                    "source_title": action.source_title,
                },
                operation_key=notion_operation_key,
            )
        except Exception as exc:
            return {
                "skipped": True,
                "reason": str(exc),
                "operation_key": notion_operation_key,
            }
        return {
            "task": asdict(result["task"]),
            "notion_task": result["notion_task"],
            "skipped": False,
        }

    @staticmethod
    def _resolve_notion_sync_statuses(statuses: Optional[list[str]]) -> list[str]:
        if not statuses:
            return ["Inbox"]
        resolved: list[str] = []
        seen: set[str] = set()
        for status in statuses:
            value = str(status).strip()
            if not value:
                continue
            normalized = value.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            resolved.append(value)
        return resolved or ["Inbox"]

    def _create_synced_notion_capture_task(
        self,
        *,
        notion_task: NotionTask,
        parent_task_id: str,
    ) -> TaskRecord:
        inferred_domain = self._infer_domain_from_notion_area(notion_task.area)
        summary = (
            f"Imported Notion task '{notion_task.title}' "
            f"(status={notion_task.status or 'unknown'})."
        )
        payload = self.create_request(
            user_request=notion_task.title or f"Synced Notion task {notion_task.page_id}",
            classification=RequestClassification(
                domain=inferred_domain,
                intent_type="capture",
                risk_level="low",
            ).validate(),
            target="notion_task_sync_item",
            request_metadata={
                "source": "notion_sync",
                "parent_task_id": parent_task_id,
                "notion_page_id": notion_task.page_id,
                "notion_status": notion_task.status,
                "notion_url": notion_task.url,
                "notion_last_edited_time": notion_task.last_edited_time,
            },
            external_ref=notion_task.page_id,
            operation_key=notion_task.operation_key,
            result_summary=summary,
            action_source="custom_adapter",
        )
        task = self.db.update_task(
            payload["task"].id,
            status="completed",
            result_summary=summary,
            external_ref=notion_task.page_id,
        )
        self._append_event(
            task_id=task.id,
            event_type="notion_sync_imported",
            payload={
                "parent_task_id": parent_task_id,
                "notion_page_id": notion_task.page_id,
                "notion_status": notion_task.status,
                "notion_last_edited_time": notion_task.last_edited_time,
            },
        )
        self._append_event(task_id=task.id, event_type="task_completed", payload={"result_summary": summary})
        return task

    @staticmethod
    def _infer_domain_from_notion_area(area: Optional[str]) -> str:
        if area is None:
            return "technical"
        normalized = area.strip().lower()
        for domain in ("personal", "technical", "finance", "system"):
            if domain in normalized:
                return domain
        return "technical"

    @staticmethod
    def _serialize_follow_up_action(action: FollowUpAction) -> dict[str, Any]:
        return {
            "title": action.title,
            "summary": action.summary,
            "domain": action.domain,
            "source_kind": action.source_kind,
            "source_title": action.source_title,
            "operation_key": action.operation_key,
            "notion_title": action.notion_title,
            "rationale": action.rationale,
        }

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

    def _require_notion_adapter(self) -> NotionAdapter:
        if self.config.notion is None:
            raise ValueError(
                f"Notion is not configured. Add {self.paths.config_path} with a notion block first."
            )
        if self._notion_adapter is None:
            self._notion_adapter = NotionAdapter(self.config.notion)
        return self._notion_adapter

    def _fail_adapter_task(
        self,
        task: TaskRecord,
        adapter_payload: dict[str, Any],
        exc: Exception,
    ) -> dict[str, Any]:
        task = self.db.update_task(task.id, status="failed", result_summary=str(exc))
        self._append_event(
            task_id=task.id,
            event_type="adapter_failed",
            payload={**adapter_payload, "error": str(exc), "error_type": type(exc).__name__},
        )
        self._append_event(task_id=task.id, event_type="task_failed", payload={"reason": str(exc)})
        raise exc

    def _map_backend_status_to_notion(self, backend_status: str) -> str:
        if backend_status not in self.config.notion.status_map:
            raise ValueError(f"no Notion status mapping configured for backend status {backend_status}")
        return self.config.notion.status_map[backend_status]

    def record_session_key(self, task_id: str, session_key: str) -> None:
        """Record the OpenClaw session key for a task's execution."""
        task = self.db.get_task(task_id)
        if not task.operation_key:
            raise ValueError(f"Task {task_id} has no operation_key")
        self.db.update_execution_session_key(operation_key=task.operation_key, session_key=session_key)

    @staticmethod
    def _serialize_notion_task(task: NotionTask) -> dict[str, Any]:
        return {
            "page_id": task.page_id,
            "url": task.url,
            "title": task.title,
            "status": task.status,
            "task_type": task.task_type,
            "area": task.area,
            "backend_task_id": task.backend_task_id,
            "operation_key": task.operation_key,
            "last_agent_update": task.last_agent_update,
            "last_edited_time": task.last_edited_time,
            "archived": task.archived,
        }

    @staticmethod
    def _today_utc_date() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    @staticmethod
    def _hours_since(iso_timestamp: str, now: datetime) -> float:
        """Return hours elapsed since an ISO timestamp string."""
        try:
            ts = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            delta = now - ts
            return delta.total_seconds() / 3600.0
        except (ValueError, TypeError):
            return 0.0

    def _find_overdue_tasks(
        self,
        *,
        domain: Optional[str] = None,
        threshold_hours: float = 48.0,
    ) -> list[dict[str, Any]]:
        """Return snapshots of tasks with no update for >threshold_hours and not in a terminal state."""
        terminal = {"completed", "failed", "cancelled", "executed"}
        all_tasks = self.list_tasks(limit=500, domain=domain)
        now_utc = datetime.now(timezone.utc)
        overdue = []
        for task in all_tasks:
            if task.status in terminal:
                continue
            hours_since_update = self._hours_since(task.updated_at, now_utc)
            if hours_since_update >= threshold_hours:
                snapshot = self._task_snapshot(task)
                snapshot["hours_since_update"] = round(hours_since_update, 1)
                overdue.append(snapshot)
        overdue.sort(key=lambda r: r["hours_since_update"], reverse=True)
        return overdue
