"""
Paperclip HTTP client.

Typed wrapper around the Paperclip REST API with:
- trusted / api-key auth
- retry on transient failures (5xx, connection errors) only
- structured error logging
- typed methods for issues, comments, documents, attachments, activity

Phase 0 scope: no checkout/release logic.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json

from .config import PaperclipConfig

log = logging.getLogger(__name__)

_TRANSIENT_STATUS_CODES = {500, 502, 503, 504}
_MAX_RETRIES = 3
_RETRY_DELAY_SECONDS = 1.0


class PaperclipError(Exception):
    """Non-transient error from the Paperclip API."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class IssueRef:
    id: str
    title: str
    status: str
    project_id: Optional[str] = None
    goal_id: Optional[str] = None
    assignee_id: Optional[str] = None
    description: Optional[str] = None
    source: Optional[str] = None
    routine_id: Optional[str] = None
    routine_run_id: Optional[str] = None
    origin_kind: Optional[str] = None


@dataclass
class CommentRef:
    id: str
    issue_id: str
    body: str


@dataclass
class DocumentRef:
    id: str
    issue_id: str
    title: str
    content: str


@dataclass
class ActivityEvent:
    id: str = ""
    issue_id: str = ""
    event_type: str = ""
    entity_type: str = ""
    entity_id: str = ""
    run_id: Optional[str] = None
    actor: Optional[str] = None
    payload: dict[str, Any] = None  # type: ignore[assignment]
    details: dict[str, Any] = None  # type: ignore[assignment]
    created_at: str = ""


class PaperclipClient:
    def __init__(self, config: PaperclipConfig) -> None:
        self._config = config
        self._base = config.base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    def create_issue(
        self,
        *,
        title: str,
        description: str,
        project_id: str,
        goal_id: str,
        assignee_id: Optional[str] = None,
        status: str = "backlog",
    ) -> IssueRef:
        body: dict[str, Any] = {
            "title": title,
            "description": description,
            "projectId": project_id,
            "goalId": goal_id,
            "status": status,
        }
        if assignee_id:
            body["assigneeAgentId"] = assignee_id
        data = self._request("POST", f"/companies/{self._config.company_id}/issues", body=body)
        return _parse_issue(data)

    def update_issue(
        self,
        issue_id: str,
        *,
        status: Optional[str] = None,
        assignee_id: Optional[str] = None,
        clear_assignee: bool = False,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> IssueRef:
        body: dict[str, Any] = {}
        if status is not None:
            body["status"] = status
        if clear_assignee:
            body["assigneeAgentId"] = None
        elif assignee_id is not None:
            body["assigneeAgentId"] = assignee_id
        if title is not None:
            body["title"] = title
        if description is not None:
            body["description"] = description
        data = self._request("PATCH", f"/issues/{issue_id}", body=body)
        return _parse_issue(data)

    def get_issue(self, issue_id: str) -> IssueRef:
        data = self._request("GET", f"/issues/{issue_id}")
        return _parse_issue(data)

    def wake_agent(
        self,
        agent_id: str,
        *,
        source: str = "assignment",
        trigger_detail: str = "system",
        reason: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        force_fresh_session: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "source": source,
            "triggerDetail": trigger_detail,
            "forceFreshSession": bool(force_fresh_session),
        }
        if reason is not None:
            body["reason"] = reason
        if payload is not None:
            body["payload"] = payload
        data = self._request("POST", f"/agents/{agent_id}/wakeup", body=body)
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    def add_comment(self, issue_id: str, body: str) -> CommentRef:
        data = self._request("POST", f"/issues/{issue_id}/comments", body={"body": body})
        return CommentRef(
            id=str(data.get("id", "")),
            issue_id=issue_id,
            body=str(data.get("body", body)),
        )

    def list_comments(self, issue_id: str) -> list[CommentRef]:
        data = self._request("GET", f"/issues/{issue_id}/comments")
        items = data if isinstance(data, list) else data.get("items", [])
        return [
            CommentRef(
                id=str(item.get("id", "")),
                issue_id=issue_id,
                body=str(item.get("body", "")),
            )
            for item in items
        ]

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    def write_document(
        self, issue_id: str, *, title: str, content: str, doc_type: str = "plan"
    ) -> DocumentRef:
        body = {"title": title, "content": content, "type": doc_type}
        data = self._request("POST", f"/issues/{issue_id}/documents", body=body)
        return DocumentRef(
            id=str(data.get("id", "")),
            issue_id=issue_id,
            title=str(data.get("title", title)),
            content=str(data.get("content", content)),
        )

    def get_document(self, issue_id: str, doc_id: str) -> DocumentRef:
        data = self._request("GET", f"/issues/{issue_id}/documents/{doc_id}")
        return DocumentRef(
            id=str(data.get("id", doc_id)),
            issue_id=issue_id,
            title=str(data.get("title", "")),
            content=str(data.get("content", "")),
        )

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    def upload_attachment(self, issue_id: str, *, filename: str, content: bytes, mime_type: str = "application/octet-stream") -> dict[str, Any]:
        # Multipart upload — encode as JSON with base64 for simplicity in phase 0
        import base64
        body = {
            "filename": filename,
            "mimeType": mime_type,
            "data": base64.b64encode(content).decode("ascii"),
        }
        return self._request("POST", f"/issues/{issue_id}/attachments", body=body)

    # ------------------------------------------------------------------
    # Activity
    # ------------------------------------------------------------------

    def list_routines(
        self,
        *,
        company_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if not company_id:
            raise PaperclipError("company_id is required to list routines")
        data = self._request("GET", f"/companies/{company_id}/routines")
        items = data if isinstance(data, list) else data.get("items", [])
        return [item for item in items if isinstance(item, dict)]

    def get_routine(self, routine_id: str) -> dict[str, Any]:
        data = self._request("GET", f"/routines/{routine_id}")
        return data if isinstance(data, dict) else {}

    def list_routine_runs(self, routine_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        data = self._request("GET", f"/routines/{routine_id}/runs?{urlencode({'limit': limit})}")
        items = data if isinstance(data, list) else data.get("items", [])
        return [item for item in items if isinstance(item, dict)]

    def list_issues(
        self,
        *,
        company_id: Optional[str] = None,
        goal_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[IssueRef]:
        """List issues for a company, optionally filtered by status.

        Note: goal_id filtering is not supported by the server's list endpoint
        and is ignored.
        """
        if not company_id:
            raise PaperclipError("company_id is required to list issues")
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        path = f"/companies/{company_id}/issues"
        if params:
            path = f"{path}?{urlencode(params)}"
        data = self._request("GET", path)
        items = data if isinstance(data, list) else data.get("items", [])
        return [_parse_issue(item) for item in items]

    def list_activity(
        self,
        issue_id: str,
        *,
        since_seconds: Optional[int] = None,
    ) -> list[ActivityEvent]:
        params: dict[str, Any] = {}
        if since_seconds is not None:
            params["lookbackSeconds"] = since_seconds
        path = f"/issues/{issue_id}/activity"
        if params:
            path = f"{path}?{urlencode(params)}"
        data = self._request("GET", path)
        items = data if isinstance(data, list) else data.get("items", [])
        return [_parse_activity(item, issue_id) for item in items]

    def list_recent_activity(
        self,
        *,
        company_id: Optional[str] = None,
        lookback_seconds: int = 300,
    ) -> list[ActivityEvent]:
        if not company_id:
            raise PaperclipError("company_id is required to list recent activity")
        params: dict[str, Any] = {"lookbackSeconds": lookback_seconds}
        path = f"/companies/{company_id}/activity?{urlencode(params)}"
        data = self._request("GET", path)
        items = data if isinstance(data, list) else data.get("items", [])
        return [_parse_activity(item, str(item.get("issueId", ""))) for item in items]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[dict[str, Any]] = None,
    ) -> Any:
        url = f"{self._base}{path}"
        headers = self._auth_headers()
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"

        encoded_body: Optional[bytes] = None
        if body is not None:
            encoded_body = json.dumps(body).encode("utf-8")

        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                req = Request(url, data=encoded_body, headers=headers, method=method)
                with urlopen(req, timeout=10) as resp:
                    raw = resp.read()
                    if not raw:
                        return {}
                    return json.loads(raw)
            except HTTPError as exc:
                # Non-transient HTTP errors (4xx) are raised immediately; 5xx are retried.
                if exc.code not in _TRANSIENT_STATUS_CODES:
                    raise PaperclipError(str(exc), status_code=exc.code) from exc
                last_exc = exc
                log.warning("paperclip transient error (attempt %d/%d): %s", attempt + 1, _MAX_RETRIES, exc)
                time.sleep(_RETRY_DELAY_SECONDS * (attempt + 1))
            except URLError as exc:
                last_exc = exc
                log.warning("paperclip transient error (attempt %d/%d): %s", attempt + 1, _MAX_RETRIES, exc)
                time.sleep(_RETRY_DELAY_SECONDS * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                log.warning("paperclip transient error (attempt %d/%d): %s", attempt + 1, _MAX_RETRIES, exc)
                time.sleep(_RETRY_DELAY_SECONDS * (attempt + 1))

        raise PaperclipError(f"Paperclip request failed after {_MAX_RETRIES} attempts: {last_exc}")

    def _auth_headers(self) -> dict[str, str]:
        if self._config.auth_mode == "trusted":
            return {"X-Trusted-Client": "agentic-os"}
        if self._config.auth_mode == "api_key":
            import os
            key = os.environ.get("PAPERCLIP_API_KEY", "")
            return {"Authorization": f"Bearer {key}"}
        return {}


# ------------------------------------------------------------------
# Parsers
# ------------------------------------------------------------------

def _parse_issue(data: dict[str, Any]) -> IssueRef:
    return IssueRef(
        id=str(data.get("id", "")),
        title=str(data.get("title", "")),
        status=str(data.get("status", "")),
        project_id=data.get("projectId") or data.get("project_id"),
        goal_id=data.get("goalId") or data.get("goal_id"),
        assignee_id=data.get("assigneeAgentId") or data.get("assigneeId") or data.get("assignee_id"),
        description=data.get("description"),
        source=data.get("source"),
        routine_id=data.get("routineId") or data.get("routine_id"),
        routine_run_id=data.get("routineRunId") or data.get("routine_run_id"),
        origin_kind=data.get("originKind") or data.get("origin_kind"),
    )


def _parse_activity(item: dict[str, Any], issue_id: str) -> ActivityEvent:
    payload = item.get("payload")
    details = item.get("details")
    if not isinstance(payload, dict):
        payload = {}
    if not isinstance(details, dict):
        details = {}
    issue_from_fields = item.get("issueId") or payload.get("issueId") or details.get("issueId")
    entity_type = str(item.get("entityType", item.get("entity_type", "")))
    entity_id = str(item.get("entityId", item.get("entity_id", "")))
    run_id_value = item.get("runId", item.get("run_id"))
    if run_id_value is None:
        run_id_value = payload.get("runId") or payload.get("run_id") or details.get("runId") or details.get("run_id")
    return ActivityEvent(
        id=str(item.get("id", "")),
        issue_id=str(issue_from_fields or issue_id or ""),
        event_type=str(item.get("eventType", item.get("type", ""))),
        entity_type=entity_type,
        entity_id=entity_id,
        run_id=str(run_id_value) if run_id_value else None,
        actor=item.get("actor") or item.get("actorId"),
        payload=payload,
        details=details,
        created_at=str(item.get("createdAt", item.get("created_at", ""))),
    )
