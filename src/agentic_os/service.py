from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

_log = logging.getLogger(__name__)

from .artifacts import ArtifactRecord, ArtifactStore
from .adapters import execute_custom_adapter
from .audit import AuditLog
from .config import AppConfig, Paths, load_app_config
from . import policy_engine
from .approval_capability import verify_approval_token
from .models import (
    ACTION_SOURCES, ApprovalRecord, InvalidTransitionError, OperatorError,
    RequestClassification, TaskRecord, normalize_action_source, validate_choice, validate_transition,
)
from .storage import Database

_RUNTIME_FOLLOWUP_INCIDENT = "incident_remediation"
_RUNTIME_FOLLOWUP_ACTIONABLE = "actionable_followup"
_RUNTIME_FOLLOWUP_KINDS = frozenset(
    {_RUNTIME_FOLLOWUP_INCIDENT, _RUNTIME_FOLLOWUP_ACTIONABLE}
)
_GITHUB_CONTRIBUTION_KIND = "github_contribution"


class AgenticOSService:
    def __init__(self, paths: Paths, config: Optional[AppConfig] = None) -> None:
        self.paths = paths
        self.config = config or load_app_config(paths)
        self.db = Database(paths.db_path)
        self.audit = AuditLog(paths.audit_log_path)
        self.artifacts = ArtifactStore(paths.artifacts_dir)
        self._cp_cache: Any = None
        self._cp_initialized: bool = False

    @property
    def _cp(self) -> Any:
        """Lazy TaskControlPlane — None when Paperclip is not configured."""
        if not self._cp_initialized:
            if self.config.paperclip is not None:
                from .task_control_plane import TaskControlPlane
                self._cp_cache = TaskControlPlane(
                    self.config.paperclip,
                    orphaned_results_dir=self.paths.data_dir / "orphaned_results",
                )
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
                _send_with_fallback(msg, subject="agentic-os startup config issue")
            except Exception as _exc:
                _log.warning("startup config notification failed: %s", _exc)

    def _transition_task(
        self, task_id: str, to_status: str, **update_kwargs
    ) -> TaskRecord:
        """Validate a status transition, update the task, and return the updated record.

        Raises InvalidTransitionError if the transition is not allowed by the
        state machine defined in models.VALID_TRANSITIONS.
        """
        task = self.db.get_task(task_id)
        validate_transition(task.status, to_status)
        return self.db.update_task(task_id, status=to_status, **update_kwargs)

    def create_request(
        self,
        *,
        user_request: str,
        classification: RequestClassification,
        agent_key: str,
        target: Optional[str] = None,
        request_metadata: Optional[dict[str, Any]] = None,
        external_write: bool = False,
        operation_key: Optional[str] = None,
        artifact_type: Optional[str] = None,
        artifact_content: Optional[Any] = None,
        result_summary: Optional[str] = None,
        external_ref: Optional[str] = None,
        action_source: Optional[str] = None,
        default_action_source: str = "manual",
        labels: Optional[list[str]] = None,
        adopt_paperclip_issue_id: Optional[str] = None,
        adopt_paperclip_routine_id: Optional[str] = None,
        adopt_paperclip_routine_run_id: Optional[str] = None,
        adopt_paperclip_origin_kind: Optional[str] = None,
    ) -> dict[str, Any]:
        effective_metadata: dict[str, Any] = dict(request_metadata) if request_metadata else {}
        action_source = normalize_action_source(
            action_source,
            request_metadata=effective_metadata,
            default=default_action_source,
        )
        validate_choice(action_source, ACTION_SOURCES, "action_source")
        effective_metadata["agent"] = agent_key

        if operation_key is not None:
            existing_tasks = self.db.list_tasks_by_operation_key(operation_key)
            if existing_tasks:
                existing_task = existing_tasks[0]
                raise ValueError(
                    f"operation_key {operation_key} is already assigned to task {existing_task.id}"
                )

        # Derive origin for policy engine from action_source
        if action_source == "paperclip_routine":
            origin = "routine"
        else:
            origin = action_source  # "manual", "api", "paperclip_manual", etc.

        verdict = policy_engine.resolve(
            origin=origin,
            domain=classification.domain,
            labels=labels or [],
        )
        policy_decision = verdict.action

        task_mode = "plan_first" if verdict.needs_plan else "direct"
        if verdict.needs_approval and not operation_key:
            raise ValueError("approval-gated requests must include an operation_key")
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
                "agent_key": agent_key,
                "origin": origin,
                "labels": labels or [],
            },
        )

        # Paperclip projection — failure must never block task creation
        cp = self._cp
        if cp is not None:
            if adopt_paperclip_issue_id:
                # Adopt an existing Paperclip issue (task was created there manually)
                try:
                    issue = cp.adopt_issue(
                        adopt_paperclip_issue_id,
                        task,
                        assignee_key=agent_key,
                    )
                    task = self.db.update_task(
                        task.id,
                        paperclip_issue_id=adopt_paperclip_issue_id,
                        paperclip_routine_id=adopt_paperclip_routine_id,
                        paperclip_routine_run_id=adopt_paperclip_routine_run_id,
                        paperclip_origin_kind=adopt_paperclip_origin_kind,
                        paperclip_project_id=(issue.project_id if issue else None),
                        paperclip_goal_id=(issue.goal_id if issue else None),
                        paperclip_assignee_agent_id=(issue.assignee_id if issue else None),
                    )
                    self._append_event(
                        task_id=task.id,
                        event_type="paperclip_issue_imported",
                        payload={"issue_id": adopt_paperclip_issue_id, "agent_key": agent_key},
                    )
                except Exception as _pc_exc:
                    self._append_event(
                        task_id=task.id,
                        event_type="paperclip_sync_failed",
                        payload={"error": str(_pc_exc), "phase": "adopt"},
                    )
            else:
                try:
                    issue = cp.create_issue(task, assignee_key=agent_key)
                    if issue and issue.id:
                        task = self.db.update_task(
                            task.id,
                            paperclip_issue_id=issue.id,
                            paperclip_origin_kind="manual_issue",
                            paperclip_project_id=issue.project_id or None,
                            paperclip_goal_id=issue.goal_id or None,
                            paperclip_assignee_agent_id=issue.assignee_id or None,
                        )
                        self._append_event(
                            task_id=task.id,
                            event_type="paperclip_issue_created",
                            payload={"issue_id": issue.id, "agent_key": agent_key},
                        )
                    else:
                        self._append_event(
                            task_id=task.id,
                            event_type="paperclip_projection_pending",
                            payload={"error": "create_issue returned None — repair job will retry"},
                        )
                except Exception as _pc_exc:
                    self._append_event(
                        task_id=task.id,
                        event_type="paperclip_projection_pending",
                        payload={"error": str(_pc_exc), "phase": "create"},
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
        if verdict.needs_approval:
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
        paperclip_status: str = "backlog",
        project_id: Optional[str] = None,
        routine_id: Optional[str] = None,
        routine_run_id: Optional[str] = None,
        origin_kind: Optional[str] = None,
        assignee_agent_id: Optional[str] = None,
        labels: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """
        Import a Paperclip issue as an agentic-os task.

        Called by the reconciler when it detects a new issue with no matching
        backend task.  Infers domain from project_id, then calls create_request()
        with adopt_paperclip_issue_id so the existing issue is adopted rather
        than a new one created.

        Returns the same dict as create_request(): {task, policy_decision, ...}
        """
        # Infer domain from project_id
        domain = "system"  # safe default
        if project_id and self.config.paperclip is not None:
            reverse_map = {v: k for k, v in self.config.paperclip.project_map.items()}
            domain = reverse_map.get(project_id, "system")

        # Idempotency: if a task already exists for this operation_key (e.g. from a
        # previous reconciler run), return it directly without re-importing.
        operation_key = f"paperclip_import:{issue_id}"
        existing = self.db.list_tasks_by_operation_key(operation_key)
        if existing:
            task = existing[0]
            return {
                "task": task,
                "policy_decision": task.policy_decision,
                "approval": None,
                "created": False,
            }

        user_request = title
        if description and description.strip() and description.strip() != title.strip():
            user_request = f"{title}\n\n{description}"

        # Derive action_source from origin_kind
        if origin_kind == "routine_execution":
            action_source = "paperclip_routine"
        else:
            action_source = "paperclip_manual"

        # Resolve agent: use Paperclip's assignee if set, else domain default
        agent_key = self._resolve_agent_key_from_paperclip(
            assignee_agent_id=assignee_agent_id, domain=domain,
        )

        initial_status = self._triage_paperclip_status(paperclip_status)

        classification = RequestClassification(
            domain=domain,
            intent_type="execute",
            risk_level="medium",
            status=initial_status,
            approval_state="not_needed",
        )

        payload = self.create_request(
            user_request=user_request,
            classification=classification,
            agent_key=agent_key,
            target=None,
            action_source=action_source,
            operation_key=operation_key,
            labels=labels,
            adopt_paperclip_issue_id=issue_id,
            adopt_paperclip_routine_id=routine_id,
            adopt_paperclip_routine_run_id=routine_run_id,
            adopt_paperclip_origin_kind=origin_kind,
        )
        task = payload["task"]
        update_kwargs: dict[str, Any] = {"paperclip_status": paperclip_status}
        if task.paperclip_issue_id != issue_id:
            update_kwargs.update(
                paperclip_issue_id=issue_id,
                paperclip_routine_id=routine_id,
                paperclip_routine_run_id=routine_run_id,
                paperclip_origin_kind=origin_kind,
            )
        task = self.db.update_task(task.id, **update_kwargs)
        payload["task"] = task
        return payload

    @staticmethod
    def _triage_paperclip_status(paperclip_status: str) -> str:
        """Determine the initial backend pipeline phase from the Paperclip
        status at import time.  This is a one-time triage, not a continuous
        mirror — after import the backend manages its own status independently.
        """
        ps = (paperclip_status or "").strip().lower()
        if ps in ("done", "cancelled"):
            return "done"
        if ps == "in_progress":
            return "in_progress"
        return "to_do"

    def _resolve_agent_key_from_paperclip(
        self,
        *,
        assignee_agent_id: Optional[str],
        domain: str,
    ) -> str:
        """Reverse-lookup agent key from Paperclip agent UUID, or fall back to domain default."""
        if assignee_agent_id and self.config.paperclip is not None:
            reverse_agent_map = {v: k for k, v in self.config.paperclip.agent_map.items()}
            agent_key = reverse_agent_map.get(assignee_agent_id)
            if agent_key:
                return agent_key
        return policy_engine.default_agent_for_domain(domain)

    def ensure_task_for_paperclip_issue(self, paperclip_issue_id: str) -> dict[str, Any]:
        """
        Resolve or import a backend task for the given Paperclip issue.

        Returns the task record plus deterministic callback instructions so the
        agent runtime never depends on a separate brief document existing.
        """
        issue_id = (paperclip_issue_id or "").strip()
        if not issue_id:
            raise ValueError("paperclip_issue_id is required")

        existing = self.db.get_task_by_paperclip_issue_id(issue_id)
        if existing is not None:
            return {
                "found": True,
                "created": False,
                "task_id": existing.id,
                "task": asdict(existing),
                "callback": self._build_callback_instructions(existing),
            }

        cp = self._cp
        if cp is None:
            raise ValueError("paperclip control plane unavailable")

        issue = cp.get_issue(issue_id)
        if issue is None:
            raise ValueError(f"paperclip issue not found: {issue_id}")

        routine_run_id = getattr(issue, "routine_run_id", None) or None
        routine_id = getattr(issue, "routine_id", None) or None
        issue_source = getattr(issue, "source", None) or ""
        issue_origin_kind = getattr(issue, "origin_kind", None) or None
        if not issue_origin_kind:
            is_routine = (
                issue_source.startswith("routine.")
                or bool(routine_run_id)
                or bool(routine_id)
            )
            issue_origin_kind = "routine_execution" if is_routine else "manual_issue"

        payload = self.import_paperclip_issue(
            issue_id=issue.id,
            title=issue.title,
            description=issue.description,
            paperclip_status=issue.status,
            project_id=issue.project_id,
            routine_id=routine_id,
            routine_run_id=routine_run_id,
            origin_kind=issue_origin_kind,
            assignee_agent_id=getattr(issue, "assignee_id", None),
            labels=getattr(issue, "labels", None) or [],
        )
        task = payload["task"]
        return {
            "found": False,
            "created": True,
            "task_id": task.id,
            "task": asdict(task),
            "callback": self._build_callback_instructions(task),
        }

    @staticmethod
    def _build_callback_instructions(task: "TaskRecord") -> dict[str, str]:
        """Deterministic callback instructions derived from the task record.

        Returned inline with the resolve response so agents never depend on a
        separate brief document existing in Paperclip.
        """
        return {
            "task_id": task.id,
            "domain": task.domain,
            "mode": task.task_mode,
            "submit_result_cmd": f"/Users/dara/agents/bin/submit-result {task.id}",
            "submit_plan_cmd": f"/Users/dara/agents/bin/submit-plan {task.id}",
            "result_file": f"/tmp/task_result_{task.id}.md",
            "plan_file": f"/tmp/task_plan_{task.id}.md",
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
        action_source: str = "tool",
        agent_key: Optional[str] = None,
    ) -> dict[str, Any]:
        payload = self.create_request(
            user_request=user_request,
            classification=classification,
            agent_key=agent_key or policy_engine.default_agent_for_domain(classification.domain),
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
        task = self.db.update_task(task.id, result_summary=summary, status="done")
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
        action_source: str = "tool",
        agent_key: Optional[str] = None,
    ) -> dict[str, Any]:
        payload = self.create_request(
            user_request=user_request,
            classification=classification,
            agent_key=agent_key or policy_engine.default_agent_for_domain(classification.domain),
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
        action_source: str = "tool",
        agent_key: Optional[str] = None,
    ) -> dict[str, Any]:
        payload = self.create_request(
            user_request=user_request,
            classification=classification,
            agent_key=agent_key or policy_engine.default_agent_for_domain(classification.domain),
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

    def create_runtime_followup_task(
        self,
        *,
        summary: str,
        kind: str = _RUNTIME_FOLLOWUP_ACTIONABLE,
        details: Optional[str] = None,
        origin_task_id: Optional[str] = None,
        origin_session_key: Optional[str] = None,
        runtime_id: Optional[str] = None,
        component: Optional[str] = None,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        risk_level: str = "medium",
        assignee_key: Optional[str] = None,
        dedupe_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Create a runtime-originated follow-up task.

        Supported kinds:
          - incident_remediation  (self-healing runtime failures)
          - actionable_followup   (general follow-up work from runtime context)
        """
        if kind not in _RUNTIME_FOLLOWUP_KINDS:
            raise ValueError(
                f"unsupported follow-up kind: {kind!r}; expected one of: "
                f"{', '.join(sorted(_RUNTIME_FOLLOWUP_KINDS))}"
            )

        if risk_level not in {"low", "medium", "high"}:
            raise ValueError("risk_level must be one of: low, medium, high")

        op_token = dedupe_key or uuid4().hex
        operation_key = f"runtime_followup:{kind}:{op_token}"
        existing = self.db.list_tasks_by_operation_key(operation_key)
        if existing:
            return {
                "task": existing[0],
                "policy_decision": existing[0].policy_decision,
                "approval": None,
                "task_mode": existing[0].task_mode,
                "deduplicated": True,
            }

        classification = RequestClassification(
            domain="system",
            intent_type="execute",
            risk_level=risk_level,
        ).validate()

        metadata: dict[str, Any] = {
            "task_kind": kind,
            "origin_task_id": origin_task_id,
            "origin_session_key": origin_session_key,
            "runtime_id": runtime_id,
            "component": component,
            "error_type": error_type,
            "error_message": error_message,
        }
        if assignee_key:
            metadata["assignee_key"] = assignee_key
        if risk_level == "high":
            # High-risk runtime remediation must still require explicit operator approval.
            metadata["require_explicit_approval"] = True

        request_lines = [
            f"Runtime follow-up ({kind})",
            "",
            f"Summary: {summary.strip()}",
        ]
        if details and details.strip():
            request_lines.extend(["", "Details:", details.strip()])
        if origin_task_id:
            request_lines.append(f"\nOrigin task: {origin_task_id}")
        if origin_session_key:
            request_lines.append(f"Origin session: {origin_session_key}")
        if runtime_id:
            request_lines.append(f"Runtime: {runtime_id}")
        if component:
            request_lines.append(f"Component: {component}")
        if error_type:
            request_lines.append(f"Error type: {error_type}")
        if error_message:
            request_lines.append(f"Error message: {error_message}")

        payload = self.create_request(
            user_request="\n".join(request_lines).strip(),
            classification=classification,
            agent_key=assignee_key or "infrastructure_engineer",
            target="runtime_followup_task",
            request_metadata=metadata,
            operation_key=operation_key,
            action_source="automation",
        )
        payload["deduplicated"] = False
        return payload

    def create_runtime_incident_task(
        self,
        *,
        summary: str,
        origin_task_id: Optional[str] = None,
        origin_session_key: Optional[str] = None,
        runtime_id: Optional[str] = None,
        component: Optional[str] = None,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        details: Optional[str] = None,
    ) -> dict[str, Any]:
        dedupe_source = "|".join(
            [
                origin_task_id or "",
                origin_session_key or "",
                runtime_id or "",
                component or "",
                error_type or "",
                error_message or "",
            ]
        )
        dedupe_key = hashlib.sha1(dedupe_source.encode("utf-8")).hexdigest()[:16]
        return self.create_runtime_followup_task(
            summary=summary,
            kind=_RUNTIME_FOLLOWUP_INCIDENT,
            details=details,
            origin_task_id=origin_task_id,
            origin_session_key=origin_session_key,
            runtime_id=runtime_id,
            component=component,
            error_type=error_type,
            error_message=error_message,
            risk_level="medium",
            dedupe_key=dedupe_key,
        )

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
        """Tasks eligible for execution."""
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
        return result

    def mark_dispatched(self, task_id: str, *, session_key: str, agent: str) -> None:
        """Record that a child session was spawned. Emits task_dispatched audit event.

        The brief document is written best-effort for operator visibility.
        Agents receive callback instructions inline from resolve-by-paperclip-issue
        and do not depend on the brief existing.
        """
        task = self.db.get_task(task_id)
        cp = self._cp
        if cp is not None and task.paperclip_issue_id:
            brief_ok = cp.ensure_task_brief(task.paperclip_issue_id, task)
            if not brief_ok:
                _log.warning(
                    "brief write failed for issue %s (task %s); dispatch continues — "
                    "agent has callback instructions from resolve response",
                    task.paperclip_issue_id, task_id,
                )
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
        if not self._is_runtime_followup_task(task):
            try:
                self.create_runtime_incident_task(
                    summary="Agent spawn failed for runtime task",
                    origin_task_id=task_id,
                    origin_session_key=task.dispatch_session_key,
                    runtime_id=task.claimed_by,
                    component="runtime_spawn",
                    error_type="spawn_failed",
                    error_message=reason,
                )
            except Exception:
                pass
        return task

    def requeue_task(self, task_id: str) -> TaskRecord:
        """
        Reset a task back to the ready state.
        Emits task_requeued audit event.
        """
        task = self.db.get_task(task_id)
        target_status = "to_do"
        self._transition_task(task_id, target_status)
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
        raise RuntimeError("plan-first lifecycle has been removed")

    def approve_plan(
        self,
        task_id: str,
        *,
        revision_id: str,
    ) -> TaskRecord:
        raise RuntimeError("plan-first lifecycle has been removed")

    def reject_plan(
        self,
        task_id: str,
        *,
        feedback: str,
    ) -> TaskRecord:
        raise RuntimeError("plan-first lifecycle has been removed")

    def reopen_plan_for_revision(
        self,
        task_id: str,
        *,
        reason: str = "Reopened in Paperclip",
    ) -> TaskRecord:
        raise RuntimeError("plan-first lifecycle has been removed")

    def cancel_task(self, task_id: str, *, reason: str = "Cancelled by operator") -> TaskRecord:
        """
        Cancel a task directly by task_id (no approval record required).

        Used by the Paperclip reconciler when an operator closes a task in Paperclip.
        Does NOT write back lifecycle status to Paperclip — the
        signal originated there, so we only update backend state.

        No-ops if task is already in a terminal state.
        """
        task = self.db.get_task(task_id)
        if task.status == "done":
            return task
        task = self._transition_task(task_id, "done", result_summary=reason)
        self._append_event(
            task_id=task_id,
            event_type="task_cancelled",
            payload={"reason": reason, "source": "paperclip_reconciler"},
        )
        return task

    def operator_close_task(self, task_id: str, *, reason: str = "Closed by operator") -> TaskRecord:
        """
        Cancel a task at operator request and close the linked Paperclip issue first.

        Unlike cancel_task() (which is used by the reconciler for Paperclip-originated
        cancellations), this path is operator-initiated. When a task has a linked
        Paperclip issue we require that cancellation to succeed before committing the
        local lifecycle change, otherwise the reconciler will immediately restore the
        backend task from the still-open Paperclip issue.
        """
        _not_closeable = {"done"}
        task = self.db.get_task(task_id)
        if task.status in _not_closeable:
            return task
        cp = self._cp
        if task.paperclip_issue_id:
            if cp is None:
                details = {
                    "task_id": task_id,
                    "paperclip_issue_id": task.paperclip_issue_id,
                    "reason": reason,
                }
                self._append_event(
                    task_id=task_id,
                    event_type="paperclip_sync_failed",
                    payload={**details, "op": "operator_close", "error": "control plane unavailable"},
                )
                raise OperatorError(
                    code="paperclip_unavailable",
                    message="Cannot close task because the linked Paperclip issue could not be reached.",
                    details=details,
                )
            closed_issue = cp.close_issue_cancelled(task.paperclip_issue_id)
            if closed_issue is None:
                details = {
                    "task_id": task_id,
                    "paperclip_issue_id": task.paperclip_issue_id,
                    "reason": reason,
                }
                self._append_event(
                    task_id=task_id,
                    event_type="paperclip_sync_failed",
                    payload={**details, "op": "operator_close", "error": "close_issue_cancelled returned None"},
                )
                raise OperatorError(
                    code="paperclip_close_failed",
                    message="Cannot close task because the linked Paperclip issue did not close.",
                    details=details,
                )
            task = self.db.update_task(
                task.id,
                paperclip_status="cancelled",
                paperclip_assignee_agent_id=closed_issue.assignee_id or None,
                paperclip_project_id=closed_issue.project_id or None,
                paperclip_goal_id=closed_issue.goal_id or None,
            )
        task = self._transition_task(task_id, "done", result_summary=reason)
        self._append_event(
            task_id=task_id,
            event_type="task_cancelled",
            payload={
                "reason": reason,
                "source": "operator",
                "paperclip_issue_id": task.paperclip_issue_id,
            },
        )
        # No Paperclip comment here: the backend posts under a trusted-client
        # identity, so any comment on an open issue with an assignee would
        # trigger Paperclip's `issue_commented` wake path.
        return task

    def bulk_close_tasks(self, *, reason: str = "Closed by operator") -> dict:
        """
        Close all tasks that are in a closeable state, syncing each to Paperclip.

        Returns a summary with lists of closed task IDs and any errors.
        """
        _not_closeable = {"done"}
        all_tasks = self.db.query_tasks(limit=1000)
        active_tasks = [t for t in all_tasks if t.status not in _not_closeable]

        closed: list[str] = []
        errors: list[dict] = []
        for task in active_tasks:
            try:
                self.operator_close_task(task.id, reason=reason)
                closed.append(task.id)
            except Exception as exc:
                errors.append({"task_id": task.id, "error": str(exc)})

        return {
            "closed": closed,
            "errors": errors,
            "closed_count": len(closed),
            "error_count": len(errors),
        }

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
            if task.status == "in_progress"
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
            "scope": "pending_approval",
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
        """Return all tasks currently in a non-terminal state."""
        tasks = [
            task for task in self.list_tasks(limit=500, domain=domain)
            if task.status in {"to_do", "in_progress"}
        ]
        return {
            "scope": "in_progress",
            "domain": domain,
            "count": len(tasks),
            "records": [self._task_snapshot(task) for task in tasks],
        }

    def recap_awaiting_input(self, *, domain: Optional[str] = None) -> dict[str, Any]:
        tasks = self.list_tasks(limit=500, status="to_do", domain=domain)
        return {
            "scope": "to_do",
            "domain": domain,
            "count": len(tasks),
            "records": [self._task_snapshot(task) for task in tasks],
        }

    def recap_failures(self, *, domain: Optional[str] = None, limit: int = 20) -> dict[str, Any]:
        tasks = self.list_tasks(limit=500, status="done", domain=domain)
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

    @staticmethod
    def _is_operation_key_conflict(error: Exception) -> bool:
        message = str(error)
        return "operation_key" in message and "already assigned to task" in message

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
        if task.policy_decision in ("approve", "approve_plan", "approval_required"):
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
            task = self.db.update_task(task.id, status="to_do", approval_state="pending")
            approval = self._create_approval_for_task(task=task, artifact=artifact)

        return {
            "task": task,
            "artifact": artifact,
            "approval": approval,
        }

    def send_approval_reminders(self, threshold_hours: float = 1.0) -> dict[str, Any]:
        """Send reminders for all pending approvals older than threshold_hours."""
        from .notification_router import route_approval_reminder
        from .notification_router import DISCORD_APPROVAL_REMINDER_PUSH_ENABLED_ENV
        import os

        if os.environ.get(DISCORD_APPROVAL_REMINDER_PUSH_ENABLED_ENV, "false").strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return {
                "reminded": 0,
                "threshold_hours": threshold_hours,
                "push_enabled": False,
                "records": [],
            }

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
            "push_enabled": True,
            "records": results,
        }

    def approve(
        self,
        approval_id: str,
        decision_note: Optional[str] = None,
        *,
        decided_by: Optional[str] = None,
        approval_token: Optional[str] = None,
    ) -> dict[str, Any]:
        self._require_approval_capability("approve", approval_id, approval_token)
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
        task = self.db.get_task(approval.task_id)
        if task.status == "done":
            task = self.db.update_task(approval.task_id, approval_state="approved")
            payload = {
                "approval": asdict(approval),
                "status_unchanged": True,
                "current_status": task.status,
            }
        else:
            task = self.db.update_task(approval.task_id, status="to_do", approval_state="approved")
            payload = {"approval": asdict(approval), "status_unchanged": False}
        payload["decided_by"] = decided_by or "unknown"
        self._append_event(task_id=task.id, event_type="approval_granted", payload=payload)
        cp = self._cp
        if cp is not None and task.paperclip_issue_id:
            cp.update_issue_status(task.paperclip_issue_id, task.status)
            self.db.update_task(task.id, paperclip_status="todo")
        return {"task": task, "approval": approval}

    def deny(
        self,
        approval_id: str,
        decision_note: Optional[str] = None,
        *,
        decided_by: Optional[str] = None,
        approval_token: Optional[str] = None,
    ) -> dict[str, Any]:
        self._require_approval_capability("deny", approval_id, approval_token)
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
            status="done",
            approval_state="denied",
            result_summary=decision_note or "Approval denied.",
        )
        self._append_event(
            task_id=task.id,
            event_type="approval_denied",
            payload={"approval": asdict(approval), "decided_by": decided_by or "unknown"},
        )
        self._append_event(
            task_id=task.id,
            event_type="task_cancelled",
            payload={"reason": task.result_summary or "Approval denied."},
        )
        cp = self._cp
        if cp is not None:
            try:
                if task.paperclip_issue_id:
                    cp.close_issue_cancelled(task.paperclip_issue_id)
                    self.db.update_task(task.id, paperclip_status="cancelled")
            except Exception as _pc_exc:
                self._append_event(
                    task_id=task.id,
                    event_type="paperclip_sync_failed",
                    payload={"error": str(_pc_exc), "op": "deny"},
                )
        return {"task": task, "approval": approval}

    def cancel(
        self,
        approval_id: str,
        decision_note: Optional[str] = None,
        *,
        decided_by: Optional[str] = None,
        approval_token: Optional[str] = None,
    ) -> dict[str, Any]:
        self._require_approval_capability("cancel", approval_id, approval_token)
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
            status="done",
            approval_state="cancelled",
            result_summary=decision_note or "Approval cancelled.",
        )
        self._append_event(
            task_id=task.id,
            event_type="approval_cancelled",
            payload={"approval": asdict(approval), "decided_by": decided_by or "unknown"},
        )
        self._append_event(
            task_id=task.id,
            event_type="task_cancelled",
            payload={"reason": task.result_summary or "Approval cancelled."},
        )
        cp = self._cp
        if cp is not None:
            try:
                if task.paperclip_issue_id:
                    cp.close_issue_cancelled(task.paperclip_issue_id)
                    self.db.update_task(task.id, paperclip_status="cancelled")
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
        if task.policy_decision in ("approve", "approve_plan", "approval_required") and task.approval_state != "approved":
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
            status="done",
            result_summary=result_summary,
        )
        task = self.db.update_task(task.id, status="done", result_summary=result_summary)
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
        task = self.db.update_task(task.id, status="done", result_summary=result_summary)
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

    def complete_task(
        self,
        task_id: str,
        result_summary: str = "",
        *,
        artifact_ref: Optional[str] = None,
    ) -> TaskRecord:
        """
        Mark task as executed with result summary and optional artifact reference.

        No Paperclip writeback happens here: the runtime (the agent session
        itself) posts canonical issue comments as its own assignee identity
        and thus is wake-safe, while the backend cannot safely comment on
        open issues. Lifecycle state is mirrored Paperclip → backend by the
        reconciler, not the reverse.
        """
        task = self.db.get_task(task_id)
        if self._is_github_contribution_task(task):
            metadata = self._request_metadata_dict(task)
            contribution = metadata.get("github_contribution")
            if not isinstance(contribution, dict) or str(contribution.get("pr_status") or "") != "merged":
                target_status = self._contribution_backend_status(
                    contribution if isinstance(contribution, dict) else {}
                )
                if task.status != target_status:
                    task = self._transition_task(
                        task_id,
                        target_status,
                        result_summary=result_summary,
                        artifact_ref=artifact_ref,
                    )
                else:
                    task = self.db.update_task(
                        task_id,
                        result_summary=result_summary,
                        artifact_ref=artifact_ref,
                    )
                self._append_event(
                    task_id=task_id,
                    event_type="github_contribution_completion_blocked",
                    payload={
                        "result_summary": result_summary,
                        "artifact_ref": artifact_ref,
                        "contribution": contribution if isinstance(contribution, dict) else {},
                    },
                )
                return task
        target_status = "done"
        task = self._transition_task(
            task_id, target_status,
            result_summary=result_summary,
            artifact_ref=artifact_ref,
        )
        self._append_event(
            task_id=task_id,
            event_type="task_completed",
            payload={"result_summary": result_summary, "artifact_ref": artifact_ref},
        )

        # Send completion notification
        try:
            from .notifier import notify_task_completed
            word_count = len(result_summary.split()) if task.intent_type == "content" else None
            notify_task_completed(task, word_count=word_count)
        except Exception as _exc:
            _log.warning("notify_task_completed failed for %s: %s", task_id, _exc)

        return task

    def record_github_contribution_result(
        self,
        task_id: str,
        *,
        result_summary: str,
        artifact_ref: Optional[str],
        contribution: dict[str, Any],
    ) -> TaskRecord:
        task = self.db.get_task(task_id)
        metadata = self._request_metadata_dict(task)
        metadata["task_kind"] = _GITHUB_CONTRIBUTION_KIND

        existing = metadata.get("github_contribution")
        merged: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
        for key, value in contribution.items():
            merged[key] = value
        if not merged.get("responsible_assignee"):
            merged["responsible_assignee"] = metadata.get("agent")
        if not merged.get("repo_strategy_path") and merged.get("repo_name"):
            merged["repo_strategy_path"] = (
                f"/Users/dara/agents/projects/technical/repo-strategy/{merged['repo_name']}.md"
            )
        metadata["github_contribution"] = merged

        target_status = self._contribution_backend_status(merged)
        request_metadata_json = self._dump_json_or_none(metadata)

        if task.status != target_status:
            task = self._transition_task(
                task_id,
                target_status,
                result_summary=result_summary,
                artifact_ref=artifact_ref,
                request_metadata_json=request_metadata_json,
            )
        else:
            task = self.db.update_task(
                task_id,
                result_summary=result_summary,
                artifact_ref=artifact_ref,
                request_metadata_json=request_metadata_json,
            )

        self._append_event(
            task_id=task_id,
            event_type="github_contribution_lifecycle_updated",
            payload={
                "result_summary": result_summary,
                "artifact_ref": artifact_ref,
                "github_contribution": merged,
                "backend_status": target_status,
            },
        )

        if target_status == "done":
            self._append_event(
                task_id=task_id,
                event_type="task_completed",
                payload={"result_summary": result_summary, "artifact_ref": artifact_ref},
            )
            try:
                from .notifier import notify_task_completed
                notify_task_completed(task)
            except Exception as _exc:
                _log.warning("notify_task_completed failed for %s: %s", task_id, _exc)

        return task

    def fail_task(self, task_id: str, reason: str) -> TaskRecord:
        task = self._transition_task(task_id, "done", result_summary=reason)
        self._append_event(task_id=task.id, event_type="task_failed", payload={"reason": reason})

        # No Paperclip comment here: see operator_close_task / write_result
        # for the rationale. Backend-posted comments on open issues would
        # wake the assignee via Paperclip's `issue_commented` path.

        # Send Discord notification
        try:
            from .notifier import notify_task_failed
            notify_task_failed(task)
        except Exception as _exc:
            _log.warning("notify_task_failed failed for %s: %s", task_id, _exc)

        return task

    def reset_task_for_retry(self, task_id: str, *, feedback: str) -> TaskRecord:
        """Reset task to in_progress for retry (max 2 retries)."""
        task = self.db.get_task(task_id)
        retry_count = (task.retry_count or 0) + 1
        
        if retry_count > 2:
            # Max retries exceeded, fail the task
            task = self.db.update_task(
                task_id,
                status="done",
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
        return {"scanned": 0, "flagged": 0, "already_stalled": 0, "threshold_hours": threshold_hours}

    def retry_task(self, task_id: str, *, feedback: str = "operator retry") -> dict:
        task = self.reset_task_for_retry(task_id, feedback=feedback)
        return {"task_id": task.id, "new_status": task.status, "retry_count": task.retry_count}

    # evaluate_policy removed — policy resolution is now handled by
    # policy_engine.resolve() called directly in create_request().

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
        except Exception as _exc:
            _log.warning("notify_approval_requested failed for %s: %s", task.id, _exc)

        return approval

    @staticmethod
    def _task_state_for_policy(policy_decision: str) -> tuple[str, str]:
        if policy_decision in ("approve", "approve_plan"):
            return ("to_do", "pending")
        return ("to_do", "not_needed")

    @staticmethod
    def _dump_json_or_none(payload: Optional[dict[str, Any]]) -> Optional[str]:
        if payload is None:
            return None
        return json.dumps(payload, sort_keys=True)

    @staticmethod
    def _request_metadata_dict(task: TaskRecord) -> dict[str, Any]:
        if not task.request_metadata_json:
            return {}
        try:
            payload = json.loads(task.request_metadata_json)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
        return {}

    def _is_runtime_followup_task(self, task: TaskRecord) -> bool:
        metadata = self._request_metadata_dict(task)
        return str(metadata.get("task_kind", "")).strip() in _RUNTIME_FOLLOWUP_KINDS

    def _is_github_contribution_task(self, task: TaskRecord) -> bool:
        metadata = self._request_metadata_dict(task)
        if str(metadata.get("task_kind", "")).strip() == _GITHUB_CONTRIBUTION_KIND:
            return True
        return isinstance(metadata.get("github_contribution"), dict)

    @staticmethod
    def _require_approval_capability(action: str, approval_id: str, token: Optional[str]) -> None:
        if verify_approval_token(action=action, approval_id=approval_id, token=token):
            return
        raise OperatorError(
            code="approval_surface_restricted",
            message=(
                "Approval mutations are restricted to the dashboard and Discord surfaces."
            ),
            details={"action": action, "approval_id": approval_id},
        )

    @staticmethod
    def _contribution_backend_status(contribution: dict[str, Any]) -> str:
        pr_status = str(contribution.get("pr_status") or "").strip()
        lifecycle_state = str(contribution.get("lifecycle_state") or "").strip()
        if pr_status == "merged" or lifecycle_state == "merged_ready_to_close":
            return "done"
        if pr_status == "closed_unmerged" or lifecycle_state == "closed_unmerged":
            return "to_do"
        return "in_progress"

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

    def _fail_adapter_task(
        self,
        task: TaskRecord,
        adapter_payload: dict[str, Any],
        exc: Exception,
    ) -> dict[str, Any]:
        task = self.db.update_task(task.id, status="done", result_summary=str(exc))
        self._append_event(
            task_id=task.id,
            event_type="adapter_failed",
            payload={**adapter_payload, "error": str(exc), "error_type": type(exc).__name__},
        )
        self._append_event(task_id=task.id, event_type="task_failed", payload={"reason": str(exc)})
        raise exc

    def record_session_key(self, task_id: str, session_key: str) -> None:
        """Record the OpenClaw session key for a task's execution."""
        task = self.db.get_task(task_id)
        if not task.operation_key:
            raise ValueError(f"Task {task_id} has no operation_key")
        self.db.update_execution_session_key(operation_key=task.operation_key, session_key=session_key)

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
        """Return snapshots of tasks with no update for >threshold_hours and not done."""
        terminal = {"done"}
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
