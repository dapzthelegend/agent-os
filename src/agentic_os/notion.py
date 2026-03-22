from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from urllib import error, parse, request

from .config import NotionConfig

DATA_SOURCE_NOTION_VERSION = "2025-09-03"


@dataclass(frozen=True)
class NotionTask:
    page_id: str
    url: str
    title: str
    status: Optional[str]
    task_type: Optional[str]
    area: Optional[str]
    backend_task_id: Optional[str]
    operation_key: Optional[str]
    last_agent_update: Optional[str]
    last_edited_time: str
    archived: bool
    raw_properties: dict[str, Any]


class NotionError(RuntimeError):
    pass


def _isoformat_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.isoformat().replace("+00:00", "Z")


class NotionAdapter:
    def __init__(self, config: NotionConfig) -> None:
        self.config = config

    def create_task(
        self,
        *,
        title: str,
        status: Optional[str] = None,
        task_type: Optional[str] = None,
        area: Optional[str] = None,
        backend_task_id: Optional[str] = None,
        operation_key: Optional[str] = None,
        last_agent_update: Optional[str] = None,
    ) -> NotionTask:
        properties = self._build_properties(
            title=title,
            status=status,
            task_type=task_type,
            area=area,
            backend_task_id=backend_task_id,
            operation_key=operation_key,
            last_agent_update=last_agent_update,
        )
        payload = {"parent": self._page_parent(), "properties": properties}
        try:
            page = self._request_json("POST", "/pages", payload)
        except NotionError as exc:
            if not self._should_retry_as_data_source(exc, payload["parent"]):
                raise
            payload["parent"] = {"data_source_id": self.config.database_id}
            page = self._request_json("POST", "/pages", payload)
        return self._normalize_page(page)

    def query_tasks(
        self,
        *,
        status: Optional[str] = None,
        updated_since: Optional[str] = None,
        limit: int = 20,
    ) -> list[NotionTask]:
        filters: list[dict[str, Any]] = []
        if status is not None:
            filters.append(
                {
                    "property": self.config.properties.status,
                    "status": {"equals": status},
                }
            )
        if updated_since is not None:
            filters.append(
                {
                    "timestamp": "last_edited_time",
                    "last_edited_time": {"on_or_after": _isoformat_timestamp(updated_since)},
                }
            )
        payload: dict[str, Any] = {"page_size": limit}
        if len(filters) == 1:
            payload["filter"] = filters[0]
        elif filters:
            payload["filter"] = {"and": filters}
        path = self._query_path()
        try:
            response = self._request_json("POST", path, payload)
        except NotionError as exc:
            fallback_path = self._fallback_query_path(exc, attempted_path=path)
            if fallback_path is None:
                raise
            response = self._request_json("POST", fallback_path, payload)
        return [self._normalize_page(item) for item in response.get("results", [])]

    def get_task(self, page_id: str) -> NotionTask:
        page = self._request_json("GET", f"/pages/{page_id}")
        return self._normalize_page(page)

    def update_task_status(
        self,
        *,
        page_id: str,
        status: str,
        last_agent_update: Optional[str] = None,
    ) -> NotionTask:
        properties = self._build_properties(status=status, last_agent_update=last_agent_update)
        page = self._request_json("PATCH", f"/pages/{page_id}", {"properties": properties})
        return self._normalize_page(page)

    def update_task_properties(
        self,
        *,
        page_id: str,
        task_type: Optional[str] = None,
        area: Optional[str] = None,
        backend_task_id: Optional[str] = None,
        operation_key: Optional[str] = None,
        last_agent_update: Optional[str] = None,
    ) -> NotionTask:
        properties = self._build_properties(
            task_type=task_type,
            area=area,
            backend_task_id=backend_task_id,
            operation_key=operation_key,
            last_agent_update=last_agent_update,
        )
        page = self._request_json("PATCH", f"/pages/{page_id}", {"properties": properties})
        return self._normalize_page(page)

    def append_note(self, *, page_id: str, note: str) -> dict[str, Any]:
        blocks = []
        for paragraph in [item.strip() for item in note.splitlines() if item.strip()]:
            for chunk in self._chunk_text(paragraph, 1800):
                blocks.append(
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [
                                {
                                    "type": "text",
                                    "text": {"content": chunk},
                                }
                            ]
                        },
                    }
                )
        if not blocks:
            raise ValueError("note must contain non-whitespace text")
        return self._request_json("PATCH", f"/blocks/{page_id}/children", {"children": blocks})

    def _build_properties(
        self,
        *,
        title: Optional[str] = None,
        status: Optional[str] = None,
        task_type: Optional[str] = None,
        area: Optional[str] = None,
        backend_task_id: Optional[str] = None,
        operation_key: Optional[str] = None,
        last_agent_update: Optional[str] = None,
    ) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        if title is not None:
            properties[self.config.properties.title] = {
                "title": [{"type": "text", "text": {"content": title}}]
            }
        if status is not None:
            properties[self.config.properties.status] = {"status": {"name": status}}
        if task_type is not None:
            properties[self.config.properties.type] = self._build_named_option_property(
                task_type,
                self.config.property_kinds.type,
            )
        if area is not None:
            properties[self.config.properties.area] = self._build_named_option_property(
                area,
                self.config.property_kinds.area,
            )
        if backend_task_id is not None:
            properties[self.config.properties.backend_task_id] = {
                "rich_text": [{"type": "text", "text": {"content": backend_task_id}}]
            }
        if operation_key is not None:
            properties[self.config.properties.operation_key] = {
                "rich_text": [{"type": "text", "text": {"content": operation_key}}]
            }
        if last_agent_update is not None:
            properties[self.config.properties.last_agent_update] = {
                "rich_text": [{"type": "text", "text": {"content": last_agent_update}}]
            }
        if not properties:
            raise ValueError("at least one Notion property update is required")
        return properties

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            parse.urljoin(f"{self.config.api_base_url.rstrip('/')}/", path.lstrip("/")),
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.config.require_api_token()}",
                "Content-Type": "application/json",
                "Notion-Version": self._request_notion_version(path, payload),
            },
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise NotionError(f"Notion API {exc.code} for {method} {path}: {detail}") from exc
        except error.URLError as exc:
            raise NotionError(f"Notion API request failed for {method} {path}: {exc.reason}") from exc

    def _normalize_page(self, page: dict[str, Any]) -> NotionTask:
        properties = page.get("properties", {})
        return NotionTask(
            page_id=str(page["id"]),
            url=str(page.get("url", "")),
            title=self._read_title(properties.get(self.config.properties.title)),
            status=self._read_status(properties.get(self.config.properties.status)),
            task_type=self._read_named_option(properties.get(self.config.properties.type)),
            area=self._read_named_option(properties.get(self.config.properties.area)),
            backend_task_id=self._read_rich_text(properties.get(self.config.properties.backend_task_id)),
            operation_key=self._read_rich_text(properties.get(self.config.properties.operation_key)),
            last_agent_update=self._read_rich_text(properties.get(self.config.properties.last_agent_update)),
            last_edited_time=str(page.get("last_edited_time", "")),
            archived=bool(page.get("archived", False)),
            raw_properties=properties,
        )

    def _page_parent(self) -> dict[str, str]:
        if self.config.data_source_id:
            return {"data_source_id": self.config.data_source_id}
        if self.config.database_id:
            return {"database_id": self.config.database_id}
        raise ValueError("Notion config requires databaseId or dataSourceId")

    def _query_path(self) -> str:
        if self.config.data_source_id:
            return f"/data_sources/{self.config.data_source_id}/query"
        if self.config.database_id:
            return f"/databases/{self.config.database_id}/query"
        raise ValueError("Notion config requires databaseId or dataSourceId")

    def _fallback_query_path(self, exc: NotionError, *, attempted_path: str) -> Optional[str]:
        if self.config.data_source_id is not None or self.config.database_id is None:
            return None
        if attempted_path != f"/databases/{self.config.database_id}/query":
            return None
        if not self._looks_like_missing_database_for_configured_id(exc):
            return None
        return f"/data_sources/{self.config.database_id}/query"

    def _should_retry_as_data_source(
        self,
        exc: NotionError,
        attempted_parent: dict[str, str],
    ) -> bool:
        if self.config.data_source_id is not None or self.config.database_id is None:
            return False
        if attempted_parent != {"database_id": self.config.database_id}:
            return False
        return self._looks_like_missing_database_for_configured_id(exc)

    def _looks_like_missing_database_for_configured_id(self, exc: NotionError) -> bool:
        if self.config.database_id is None:
            return False
        message = str(exc)
        pattern = rf"Could not find database with ID: {re.escape(self.config.database_id)}\b"
        return "code\":\"object_not_found\"" in message and re.search(pattern, message) is not None

    @staticmethod
    def _read_title(property_value: Optional[dict[str, Any]]) -> str:
        items = (property_value or {}).get("title", [])
        return "".join(item.get("plain_text", "") for item in items)

    @staticmethod
    def _read_status(property_value: Optional[dict[str, Any]]) -> Optional[str]:
        status = (property_value or {}).get("status")
        if not status:
            return None
        return status.get("name")

    @staticmethod
    def _read_named_option(property_value: Optional[dict[str, Any]]) -> Optional[str]:
        select = (property_value or {}).get("select")
        if select:
            return select.get("name")
        multi_select = (property_value or {}).get("multi_select", [])
        if not multi_select:
            return None
        names = [item.get("name", "") for item in multi_select if item.get("name")]
        if not names:
            return None
        return ", ".join(names)

    @staticmethod
    def _read_rich_text(property_value: Optional[dict[str, Any]]) -> Optional[str]:
        items = (property_value or {}).get("rich_text", [])
        text = "".join(item.get("plain_text", "") for item in items)
        return text or None

    @staticmethod
    def _build_named_option_property(value: str, kind: str) -> dict[str, Any]:
        if kind == "multi_select":
            return {"multi_select": [{"name": value}]}
        return {"select": {"name": value}}

    def _request_notion_version(
        self,
        path: str,
        payload: Optional[dict[str, Any]],
    ) -> str:
        if self._uses_data_source_shape(path, payload):
            return max(self.config.notion_version, DATA_SOURCE_NOTION_VERSION)
        return self.config.notion_version

    @staticmethod
    def _uses_data_source_shape(path: str, payload: Optional[dict[str, Any]]) -> bool:
        if path.startswith("/data_sources/"):
            return True
        parent = (payload or {}).get("parent")
        return isinstance(parent, dict) and "data_source_id" in parent

    @staticmethod
    def _chunk_text(value: str, size: int) -> list[str]:
        return [value[index : index + size] for index in range(0, len(value), size)]
