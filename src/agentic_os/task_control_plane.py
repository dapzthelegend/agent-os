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
    "planning": "in_progress",
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
    # Routing helpers
    # ------------------------------------------------------------------

    def resolve_assignee_for_task(self, task: TaskRecord) -> str:
        """Return the agent key to assign based on task domain and mode."""
        if task.task_mode == "plan_first":
            return _COORDINATION_AGENT
        return _DOMAIN_AGENT_MAP.get(task.domain, _ENGINEER_AGENT)

    def resolve_executor_key(self, task: TaskRecord) -> str:
        """
        Return the executor agent key for a completing task.

        Mirrors the Phase 3 assignment policy:
          content intent          → content_writer
          finance domain          → accountant
          admin/scheduling intent → executive_assistant
          technical domain        → engineer (default; executor_codex not yet auto-detected)
          everything else         → engineer
        """
        if task.intent_type == "content":
            return _WRITING_AGENT
        if task.domain == "finance":
            return _FINANCE_AGENT
        if task.intent_type in ("capture", "recap") and task.domain == "system":
            return _ADMIN_AGENT
        return _ENGINEER_AGENT

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
