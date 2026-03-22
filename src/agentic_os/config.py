from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class Paths:
    root: Path
    data_dir: Path
    artifacts_dir: Path
    db_path: Path
    audit_log_path: Path
    policy_rules_path: Path
    config_path: Path

    @classmethod
    def from_root(cls, root: Path) -> "Paths":
        data_dir = root / "data"
        artifacts_dir = root / "artifacts"
        return cls(
            root=root,
            data_dir=data_dir,
            artifacts_dir=artifacts_dir,
            db_path=data_dir / "agentic_os.sqlite3",
            audit_log_path=data_dir / "audit_log.jsonl",
            policy_rules_path=root / "policy_rules.json",
            config_path=root / "agentic_os.config.json",
        )


@dataclass(frozen=True)
class NotionPropertyMap:
    title: str = "Title"
    status: str = "Status"
    type: str = "Type"
    area: str = "Area"
    backend_task_id: str = "OpenClaw Task ID"
    operation_key: str = "Operation Key"
    last_agent_update: str = "Last Agent Update"


@dataclass(frozen=True)
class NotionPropertyKindMap:
    type: str = "select"
    area: str = "select"


@dataclass(frozen=True)
class NotionConfig:
    api_token_env: str
    database_id: Optional[str]
    data_source_id: Optional[str]
    properties: NotionPropertyMap
    property_kinds: NotionPropertyKindMap
    status_map: dict[str, str]
    api_base_url: str = "https://api.notion.com/v1"
    notion_version: str = "2022-06-28"

    def require_api_token(self) -> str:
        token = os.environ.get(self.api_token_env)
        if not token:
            raise ValueError(
                f"Notion API token env var {self.api_token_env} is not set"
            )
        return token


@dataclass(frozen=True)
class AppConfig:
    notion: Optional[NotionConfig] = None


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_paths() -> Paths:
    return Paths.from_root(repo_root())


def load_app_config(paths: Paths) -> AppConfig:
    config_path = Path(os.environ.get("AGENTIC_OS_CONFIG_PATH", str(paths.config_path)))
    if not config_path.exists():
        return AppConfig()
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    notion_payload = payload.get("notion")
    if notion_payload is None:
        return AppConfig()
    properties_payload = notion_payload.get("properties", {})
    property_kinds_payload = notion_payload.get("propertyKinds", {})
    status_map = notion_payload.get("statusMap", {})
    if not isinstance(property_kinds_payload, dict):
        raise ValueError("notion.propertyKinds must be an object")
    if not isinstance(status_map, dict):
        raise ValueError("notion.statusMap must be an object")
    database_id = notion_payload.get("databaseId")
    data_source_id = notion_payload.get("dataSourceId")
    if database_id is None and data_source_id is None:
        raise ValueError("notion.databaseId or notion.dataSourceId is required")
    type_kind = str(property_kinds_payload.get("type", "select"))
    area_kind = str(property_kinds_payload.get("area", "select"))
    valid_property_kinds = {"select", "multi_select"}
    if type_kind not in valid_property_kinds:
        raise ValueError("notion.propertyKinds.type must be 'select' or 'multi_select'")
    if area_kind not in valid_property_kinds:
        raise ValueError("notion.propertyKinds.area must be 'select' or 'multi_select'")
    return AppConfig(
        notion=NotionConfig(
            api_token_env=str(notion_payload.get("apiTokenEnv", "NOTION_API_KEY")),
            database_id=str(database_id) if database_id is not None else None,
            data_source_id=(
                str(data_source_id) if data_source_id is not None else None
            ),
            properties=NotionPropertyMap(
                title=str(properties_payload.get("title", "Title")),
                status=str(properties_payload.get("status", "Status")),
                type=str(properties_payload.get("type", "Type")),
                area=str(properties_payload.get("area", "Area")),
                backend_task_id=str(
                    properties_payload.get("backendTaskId", "OpenClaw Task ID")
                ),
                operation_key=str(properties_payload.get("operationKey", "Operation Key")),
                last_agent_update=str(
                    properties_payload.get("lastAgentUpdate", "Last Agent Update")
                ),
            ),
            property_kinds=NotionPropertyKindMap(
                type=type_kind,
                area=area_kind,
            ),
            status_map={str(key): str(value) for key, value in status_map.items()},
            api_base_url=str(notion_payload.get("apiBaseUrl", "https://api.notion.com/v1")),
            notion_version=str(notion_payload.get("notionVersion", "2022-06-28")),
        )
    )


def load_policy_rules(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rules = payload.get("rules")
    if not isinstance(rules, list):
        raise ValueError(f"policy rules file must contain a top-level 'rules' list: {path}")
    return rules
