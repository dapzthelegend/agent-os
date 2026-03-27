from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


DOMAINS = ("personal", "technical", "finance", "system")
INTENT_TYPES = ("read", "draft", "execute", "capture", "recap", "content")
RISK_LEVELS = ("low", "medium", "high")
ACTION_SOURCES = ("openclaw_tool", "openclaw_skill", "custom_adapter", "manual", "api", "paperclip_manual")
STATUSES = (
    # legacy live-path statuses (phase 0 compat)
    "new",
    "in_progress",
    "awaiting_input",
    "awaiting_approval",
    "approved",
    "executed",
    # new statuses (phase 1+)
    "planning",
    "awaiting_plan_review",
    "approved_for_execution",
    "executing",
    # terminal
    "completed",
    "failed",
    "cancelled",
    "stalled",
)

TASK_MODES = ("direct", "plan_first")
APPROVAL_STATES = ("not_needed", "pending", "approved", "denied", "cancelled")
POLICY_DECISIONS = ("read_ok", "draft_required", "approval_required")
APPROVAL_RECORD_STATES = ("pending", "approved", "denied", "cancelled")
APPROVAL_SUBJECT_TYPES = ("artifact", "action")
EXECUTION_STATES = ("executed", "duplicate_rejected")


def validate_choice(value: str, allowed: tuple[str, ...], field_name: str) -> str:
    if value not in allowed:
        allowed_values = ", ".join(allowed)
        raise ValueError(f"{field_name} must be one of: {allowed_values}")
    return value


@dataclass(frozen=True)
class RequestClassification:
    domain: str
    intent_type: str
    risk_level: str
    status: str = "new"
    approval_state: str = "not_needed"

    def validate(self) -> "RequestClassification":
        validate_choice(self.domain, DOMAINS, "domain")
        validate_choice(self.intent_type, INTENT_TYPES, "intent_type")
        validate_choice(self.risk_level, RISK_LEVELS, "risk_level")
        validate_choice(self.status, STATUSES, "status")
        validate_choice(self.approval_state, APPROVAL_STATES, "approval_state")
        return self


@dataclass(frozen=True)
class TaskRecord:
    id: str
    created_at: str
    updated_at: str
    domain: str
    intent_type: str
    risk_level: str
    status: str
    approval_state: str
    user_request: str
    result_summary: Optional[str]
    artifact_ref: Optional[str]
    external_ref: Optional[str]
    target: Optional[str]
    request_metadata_json: Optional[str]
    operation_key: Optional[str]
    external_write: bool
    policy_decision: Optional[str]
    action_source: str
    retry_count: int = 0
    claimed_at: Optional[str] = None
    claimed_by: Optional[str] = None
    dispatch_session_key: Optional[str] = None
    dispatch_attempts: int = 0
    # Paperclip / new-schema fields (phase 0+)
    title: Optional[str] = None
    description: Optional[str] = None
    task_mode: str = "direct"
    delivery_target: Optional[str] = None
    delivery_thread_id: Optional[str] = None
    artifact_path: Optional[str] = None
    paperclip_issue_id: Optional[str] = None
    paperclip_assignee_agent_id: Optional[str] = None
    paperclip_project_id: Optional[str] = None
    paperclip_goal_id: Optional[str] = None
    plan_version: int = 0
    approved_plan_revision_id: Optional[str] = None


@dataclass(frozen=True)
class ApprovalRecord:
    id: str
    task_id: str
    status: str
    subject_type: str
    artifact_id: Optional[str]
    action_target: Optional[str]
    operation_key: Optional[str]
    payload_json: str
    decision_note: Optional[str]
    created_at: str
    updated_at: str
    decided_at: Optional[str]

    def validate(self) -> "ApprovalRecord":
        validate_choice(self.status, APPROVAL_RECORD_STATES, "status")
        validate_choice(self.subject_type, APPROVAL_SUBJECT_TYPES, "subject_type")
        return self


@dataclass(frozen=True)
class ExecutionRecord:
    operation_key: str
    task_id: str
    approval_id: Optional[str]
    status: str
    result_summary: Optional[str]
    created_at: str
    updated_at: str
    session_key: Optional[str] = None

    def validate(self) -> "ExecutionRecord":
        validate_choice(self.status, EXECUTION_STATES, "status")
        return self


@dataclass(frozen=True)
class OperatorError(Exception):
    code: str
    message: str
    details: Optional[dict[str, Any]] = None

    def __str__(self) -> str:
        return self.message
