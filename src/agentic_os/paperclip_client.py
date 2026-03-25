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
from urllib.error import URLError
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
    id: str
    issue_id: str
    event_type: str
    actor: Optional[str]
    payload: dict[str, Any]
    created_at: str


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
        status: str = "todo",
    ) -> IssueRef:
        body: dict[str, Any] = {
            "title": title,
            "description": description,
            "projectId": project_id,
            "goalId": goal_id,
            "status": status,
        }
        if assignee_id:
            body["assigneeId"] = assignee_id
        data = self._request("POST", "/issues", body=body)
        return _parse_issue(data)

    def update_issue(
        self,
        issue_id: str,
        *,
        status: Optional[str] = None,
        assignee_id: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> IssueRef:
        body: dict[str, Any] = {}
        if status is not None:
            body["status"] = status
        if assignee_id is not None:
            body["assigneeId"] = assignee_id
        if title is not None:
            body["title"] = title
        if description is not None:
            body["description"] = description
        data = self._request("PATCH", f"/issues/{issue_id}", body=body)
        return _parse_issue(data)

    def get_issue(self, issue_id: str) -> IssueRef:
        data = self._request("GET", f"/issues/{issue_id}")
        return _parse_issue(data)

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
        params: dict[str, Any] = {"lookbackSeconds": lookback_seconds}
        if company_id:
            params["companyId"] = company_id
        path = f"/activity?{urlencode(params)}"
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
            except URLError as exc:
                last_exc = exc
                log.warning("paperclip transient error (attempt %d/%d): %s", attempt + 1, _MAX_RETRIES, exc)
                time.sleep(_RETRY_DELAY_SECONDS * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                # Check if it's an HTTP error with a status code
                status_code: Optional[int] = getattr(getattr(exc, "code", None), "__int__", lambda: None)()
                if status_code is None:
                    status_code = getattr(exc, "code", None)
                if status_code is not None and status_code not in _TRANSIENT_STATUS_CODES:
                    raise PaperclipError(str(exc), status_code=status_code) from exc
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
        assignee_id=data.get("assigneeId") or data.get("assignee_id"),
    )


def _parse_activity(item: dict[str, Any], issue_id: str) -> ActivityEvent:
    return ActivityEvent(
        id=str(item.get("id", "")),
        issue_id=str(item.get("issueId", issue_id)),
        event_type=str(item.get("eventType", item.get("type", ""))),
        actor=item.get("actor") or item.get("actorId"),
        payload=item.get("payload", {}),
        created_at=str(item.get("createdAt", item.get("created_at", ""))),
    )
