"""
Task control plane — Paperclip projection and review surface.

Wraps all Paperclip interactions for the backend:
- create/update issue projections
- add comments
- write plan and result documents
- upload artifacts
- poll activity

All failures are logged and swallowed so Paperclip outages never crash backend flows.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .config import PaperclipConfig
from .models import TaskRecord
from .paperclip_client import (
    ActivityEvent,
    CommentRef,
    DocumentRef,
    IssueRef,
    PaperclipClient,
    PaperclipError,
)

log = logging.getLogger(__name__)

_MIME_MAP: dict[str, str] = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".py": "text/x-python",
    ".html": "text/html",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".csv": "text/csv",
}


def _mime_for_path(path: Path) -> str:
    return _MIME_MAP.get(path.suffix.lower(), "application/octet-stream")

# ------------------------------------------------------------------
# Backend → Paperclip status map
# ------------------------------------------------------------------

_STATUS_MAP: dict[str, str] = {
    "new": "todo",
    # Planning work should be pickup-eligible for execution agents.
    "planning": "todo",
    "awaiting_plan_review": "in_review",
    "approved_for_execution": "todo",
    "executing": "in_progress",
    "in_progress": "in_progress",
    "awaiting_approval": "blocked",
    "awaiting_input": "blocked",
    "completed": "done",
    "executed": "done",
    "failed": "blocked",
    "stalled": "blocked",
    "cancelled": "cancelled",
}

# ------------------------------------------------------------------
# Default routing: domain/context → agent key
# ------------------------------------------------------------------

_DOMAIN_AGENT_MAP: dict[str, str] = {
    "personal": "executive_assistant",
    "technical": "engineer",
    "finance": "accountant",
    "system": "project_manager",
}

_COORDINATION_AGENT = "project_manager"
_ESCALATION_AGENT = "chief_of_staff"
_ENGINEER_AGENT = "engineer"
_CODEX_AGENT = "executor_codex"
_WRITING_AGENT = "content_writer"
_FINANCE_AGENT = "accountant"
_ADMIN_AGENT = "executive_assistant"
_INFRA_ENGINEER_AGENT = "infrastructure_engineer"


class TaskControlPlane:
    def __init__(self, config: PaperclipConfig) -> None:
        self._config = config
        self._client = PaperclipClient(config)

    # ------------------------------------------------------------------
    # Issue projection
    # ------------------------------------------------------------------

    def create_issue(
        self,
        task: TaskRecord,
        *,
        assignee_key: Optional[str] = None,
    ) -> Optional[IssueRef]:
        """Create a Paperclip issue for a backend task. Returns None on failure."""
        try:
            project_id = self._resolve_project(task.domain)
            assignee_id = self._resolve_agent(assignee_key or _COORDINATION_AGENT)
            paperclip_status = _STATUS_MAP.get(task.status, "todo")
            title = task.title or task.user_request[:120]
            description = task.description or task.user_request
            return self._client.create_issue(
                title=title,
                description=description,
                project_id=project_id,
                goal_id=self._config.goal_id,
                assignee_id=assignee_id,
                status=paperclip_status,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("control_plane.create_issue failed for task %s: %s", task.id, exc)
            return None

    def update_issue_status(
        self,
        issue_id: str,
        backend_status: str,
        *,
        assignee_key: Optional[str] = None,
    ) -> Optional[IssueRef]:
        """Sync backend status to Paperclip. Returns None on failure."""
        try:
            paperclip_status = _STATUS_MAP.get(backend_status, "todo")
            kwargs: dict = {"status": paperclip_status}
            if assignee_key:
                kwargs["assignee_id"] = self._resolve_agent(assignee_key)
            return self._client.update_issue(issue_id, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.error("control_plane.update_issue_status failed for issue %s: %s", issue_id, exc)
            return None

    def get_issue(self, issue_id: str) -> Optional[IssueRef]:
        """Fetch a Paperclip issue by id. Returns None on failure."""
        try:
            return self._client.get_issue(issue_id)
        except Exception as exc:  # noqa: BLE001
            log.error("control_plane.get_issue failed for issue %s: %s", issue_id, exc)
            return None

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    def add_comment(self, issue_id: str, body: str) -> Optional[CommentRef]:
        try:
            return self._client.add_comment(issue_id, body)
        except Exception as exc:  # noqa: BLE001
            log.error("control_plane.add_comment failed for issue %s: %s", issue_id, exc)
            return None

    def post_result_comment(self, issue_id: str, result_summary: str) -> Optional[CommentRef]:
        return self.add_comment(issue_id, f"**Result:** {result_summary}")

    def post_failure_comment(self, issue_id: str, reason: str) -> Optional[CommentRef]:
        return self.add_comment(issue_id, f"**Failed:** {reason}")

    def list_comments(self, issue_id: str) -> list[CommentRef]:
        try:
            return self._client.list_comments(issue_id)
        except Exception as exc:  # noqa: BLE001
            log.error("control_plane.list_comments failed for issue %s: %s", issue_id, exc)
            return []

    # ------------------------------------------------------------------
    # Plan documents
    # ------------------------------------------------------------------

    def write_plan_doc(
        self, issue_id: str, plan_text: str, *, version: int = 1
    ) -> Optional[DocumentRef]:
        try:
            return self._client.write_document(
                issue_id,
                title=f"Plan v{version}",
                content=plan_text,
                doc_type="plan",
            )
        except Exception as exc:  # noqa: BLE001
            log.error("control_plane.write_plan_doc failed for issue %s: %s", issue_id, exc)
            return None

    def get_document(self, issue_id: str, doc_id: str) -> Optional[DocumentRef]:
        try:
            return self._client.get_document(issue_id, doc_id)
        except Exception as exc:  # noqa: BLE001
            log.error("control_plane.get_document failed for issue %s/%s: %s", issue_id, doc_id, exc)
            return None

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------

    def upload_artifact(
        self, issue_id: str, artifact_path: Path, *, mime_type: str = "application/octet-stream"
    ) -> Optional[dict]:
        try:
            content = artifact_path.read_bytes()
            return self._client.upload_attachment(
                issue_id,
                filename=artifact_path.name,
                content=content,
                mime_type=mime_type,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("control_plane.upload_artifact failed for issue %s: %s", issue_id, exc)
            return None

    # ------------------------------------------------------------------
    # Activity polling
    # ------------------------------------------------------------------

    def poll_activity(
        self,
        issue_id: str,
        *,
        lookback_seconds: Optional[int] = None,
    ) -> list[ActivityEvent]:
        try:
            seconds = lookback_seconds or self._config.reconcile_activity_lookback_seconds
            return self._client.list_activity(issue_id, since_seconds=seconds)
        except Exception as exc:  # noqa: BLE001
            log.error("control_plane.poll_activity failed for issue %s: %s", issue_id, exc)
            return []

    def poll_company_activity(self) -> list[ActivityEvent]:
        try:
            return self._client.list_recent_activity(
                company_id=self._config.company_id,
                lookback_seconds=self._config.reconcile_activity_lookback_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("control_plane.poll_company_activity failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Execution result writeback (Phase 3)
    # ------------------------------------------------------------------

    # Results shorter than this are written as a comment; longer ones get a document.
    LONG_RESULT_THRESHOLD = 500

    def write_result(
        self,
        issue_id: str,
        result_text: str,
        *,
        task_id: str,
        artifact_path: Optional[Path] = None,
    ) -> None:
        """
        Full Paperclip writeback for an execution result.

        - Uploads artifact attachment if artifact_path is provided and exists.
        - Short result (≤LONG_RESULT_THRESHOLD chars): posts a comment.
        - Long result (>LONG_RESULT_THRESHOLD chars): writes a result document
          and posts a short comment with a preview.

        All sub-operations are independently fault-tolerant.
        """
        # 1. Upload artifact attachment
        if artifact_path is not None:
            try:
                p = Path(artifact_path) if not isinstance(artifact_path, Path) else artifact_path
                if p.exists():
                    mime = _mime_for_path(p)
                    self.upload_artifact(issue_id, p, mime_type=mime)
                    self.add_comment(issue_id, f"Artifact attached: `{p.name}` (task {task_id})")
            except Exception as exc:  # noqa: BLE001
                log.error("control_plane.write_result artifact upload failed for %s: %s", issue_id, exc)

        # 2. Write result text
        if len(result_text) > self.LONG_RESULT_THRESHOLD:
            try:
                self._client.write_document(
                    issue_id,
                    title="Result",
                    content=result_text,
                    doc_type="result",
                )
                preview = result_text[:200].rstrip() + "…"
                self.add_comment(issue_id, f"**Result** (full text in document):\n{preview}")
            except Exception as exc:  # noqa: BLE001
                log.error("control_plane.write_result doc write failed for %s: %s", issue_id, exc)
                # Fallback: post full text as comment (truncated to Paperclip limit)
                self.add_comment(issue_id, f"**Result:** {result_text[:1000]}")
        else:
            self.post_result_comment(issue_id, result_text)

    # ------------------------------------------------------------------
    # Paperclip-originated task adoption
    # ------------------------------------------------------------------

    def adopt_issue(
        self,
        issue_id: str,
        task: "TaskRecord",
        *,
        assignee_key: Optional[str] = None,
    ) -> Optional[IssueRef]:
        """
        Adopt an existing Paperclip issue for a newly-imported backend task.

        - Assigns the appropriate agent
        - Updates the issue status to match the backend task status
        - Posts a comment noting that the task has been registered in agentic-os
        - Writes the callback instructions as a brief document

        Returns the updated IssueRef, or None on failure.
        """
        try:
            paperclip_status = _STATUS_MAP.get(task.status, "todo")
            resolved_key = assignee_key or self.resolve_assignee_for_task(task)
            agent_id = self._resolve_agent(resolved_key)
            updated = self._client.update_issue(
                issue_id,
                status=paperclip_status,
                assignee_id=agent_id,
            )
            self._write_import_brief(issue_id, task)
            return updated
        except Exception as exc:  # noqa: BLE001
            log.error("control_plane.adopt_issue failed for issue %s: %s", issue_id, exc)
            return None

    def _write_import_brief(self, issue_id: str, task: "TaskRecord") -> None:
        """Write the agentic-os callback instructions to a Paperclip-originated issue."""
        try:
            callback_instructions = (
                f"Write your result to `/tmp/task_result_{task.id}.txt` with "
                f"`RESULT_START` / `RESULT_END` / `TASK_DONE: {task.id}` markers, then run:\n\n"
                f"`/Users/dara/agents/bin/submit-result {task.id}`"
            )
            if task.task_mode == "plan_first" and task.status in ("new", "planning"):
                callback_instructions = (
                    f"Write your plan to `/tmp/task_plan_{task.id}.txt`, then run:\n\n"
                    f"`/Users/dara/agents/bin/submit-plan {task.id}`\n\n"
                    f"Stop after submission and wait for PM review."
                )

            brief = (
                f"# agentic-os task registered\n\n"
                f"**Task ID:** `{task.id}`  \n"
                f"**Domain:** {task.domain}  \n"
                f"**Mode:** {task.task_mode}  \n\n"
                f"## Instructions\n\n"
                f"{callback_instructions}\n\n"
                f"Session key fallback for callbacks is automatic: "
                f"`OPENCLAW_SESSION_ID` → `PAPERCLIP_RUN_ID`."
            )
            self._client.write_document(
                issue_id,
                title="agentic-os brief",
                content=brief,
                doc_type="brief",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("control_plane._write_import_brief failed for issue %s: %s", issue_id, exc)

    def list_all_issues(
        self,
        *,
        goal_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[IssueRef]:
        """List Paperclip issues for this company (used for safety-net scanning)."""
        try:
            return self._client.list_issues(
                company_id=self._config.company_id,
                goal_id=goal_id or self._config.goal_id,
                status=status,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("control_plane.list_all_issues failed: %s", exc)
            return []

    def list_routine_runs(
        self,
        routine_id: str,
        *,
        limit: int = 50,
    ) -> list[dict]:
        try:
            return self._client.list_routine_runs(routine_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            log.error("control_plane.list_routine_runs failed for routine %s: %s", routine_id, exc)
            return []

    def get_routine(self, routine_id: str) -> Optional[dict]:
        try:
            return self._client.get_routine(routine_id)
        except Exception as exc:  # noqa: BLE001
            log.error("control_plane.get_routine failed for routine %s: %s", routine_id, exc)
            return None

    # ------------------------------------------------------------------
    # Routing helpers
    # ------------------------------------------------------------------

    def resolve_assignee_for_task(self, task: TaskRecord) -> str:
        """Return the agent key to assign at task creation time.

        For plan_first tasks the execution agent is assigned immediately — they
        write the plan first, then execute after PM approval.  The PM is brought
        in only at the review step (submit_plan) and is never the plan author.
        """
        return self.resolve_executor_key(task)

    def resolve_executor_key(self, task: TaskRecord) -> str:
        """
        Return the executor agent key for a completing task.

        Assignment policy:
          content intent          → content_writer
          finance domain          → accountant
          admin/scheduling intent → executive_assistant
          classifier agent=codex  → executor_codex
          everything else         → engineer
        """
        metadata = self._task_metadata(task)

        preferred_assignee = str(metadata.get("assignee_key", "")).strip()
        if preferred_assignee and preferred_assignee in self._config.agent_map:
            return preferred_assignee

        task_kind = str(metadata.get("task_kind", "")).strip()
        if task_kind == "incident_remediation":
            if _INFRA_ENGINEER_AGENT in self._config.agent_map:
                return _INFRA_ENGINEER_AGENT
            return _ENGINEER_AGENT

        if task.intent_type == "content":
            return _WRITING_AGENT
        if task.domain == "finance":
            return _FINANCE_AGENT
        if task.intent_type in ("capture", "recap") and task.domain == "system":
            return _ADMIN_AGENT
        # Route to Codex if the intake classifier selected it
        classifier_agent = ""
        classifier_agent = str(metadata.get("agent", "")).strip()
        if classifier_agent == "codex":
            return _CODEX_AGENT
        return _ENGINEER_AGENT

    @staticmethod
    def _task_metadata(task: TaskRecord) -> dict:
        if not task.request_metadata_json:
            return {}
        try:
            payload = json.loads(task.request_metadata_json)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
        return {}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve_project(self, domain: str) -> str:
        project_id = self._config.project_map.get(domain, "")
        if not project_id:
            raise PaperclipError(f"No project_map entry for domain: {domain!r}")
        return project_id

    def _resolve_agent(self, agent_key: str) -> str:
        agent_id = self._config.agent_map.get(agent_key, "")
        if not agent_id:
            raise PaperclipError(f"No agent_map entry for key: {agent_key!r}")
        return agent_id
