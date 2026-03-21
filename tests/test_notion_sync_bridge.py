from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from agentic_os.config import (
    AppConfig,
    NotionConfig,
    NotionPropertyKindMap,
    NotionPropertyMap,
    Paths,
)
from agentic_os.models import RequestClassification
from agentic_os.notion import NotionTask
from agentic_os.service import AgenticOSService


class _FakeNotionAdapter:
    def __init__(self, rows_by_status: dict[str, list[NotionTask]]) -> None:
        self.rows_by_status = rows_by_status

    def query_tasks(
        self,
        *,
        status: Optional[str] = None,
        updated_since: Optional[str] = None,
        limit: int = 20,
    ) -> list[NotionTask]:
        _ = updated_since
        if status is None:
            rows: list[NotionTask] = []
            for items in self.rows_by_status.values():
                rows.extend(items)
            return rows[:limit]
        return self.rows_by_status.get(status, [])[:limit]


def _notion_task(*, page_id: str, title: str, status: str, operation_key: Optional[str] = None) -> NotionTask:
    return NotionTask(
        page_id=page_id,
        url=f"https://www.notion.so/{page_id}",
        title=title,
        status=status,
        task_type="task",
        area="technical",
        backend_task_id=None,
        operation_key=operation_key,
        last_agent_update=None,
        last_edited_time="2026-03-21T08:00:00Z",
        archived=False,
        raw_properties={},
    )


class NotionSyncBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.paths = Paths.from_root(self.root)
        self.paths.policy_rules_path.write_text(
            json.dumps(
                {
                    "rules": [
                        {"match": {"external_write": True}, "action": "approval_required"},
                        {"match": {"intent_type": "execute"}, "action": "approval_required"},
                        {"match": {"intent_type": "draft"}, "action": "draft_required"},
                        {"match": {"intent_type": "read"}, "action": "read_ok"},
                        {"match": {"intent_type": "capture"}, "action": "read_ok"},
                        {"match": {"intent_type": "recap"}, "action": "read_ok"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        config = AppConfig(
            notion=NotionConfig(
                api_token_env="NOTION_API_KEY",
                database_id="fake_db",
                data_source_id=None,
                properties=NotionPropertyMap(),
                property_kinds=NotionPropertyKindMap(),
                status_map={"completed": "Done"},
            )
        )
        self.service = AgenticOSService(self.paths, config=config)
        self.service.initialize()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_sync_imports_unseen_task_then_deduplicates_by_external_ref(self) -> None:
        self.service._notion_adapter = _FakeNotionAdapter(
            {"Inbox": [_notion_task(page_id="pg_1", title="Draft launch checklist", status="Inbox")]}
        )
        classification = RequestClassification(domain="system", intent_type="read", risk_level="low").validate()

        first = self.service.sync_notion_tasks(
            user_request="Sync Notion intake",
            classification=classification,
            statuses=["Inbox"],
            limit=10,
        )
        self.assertEqual(first["imported_count"], 1)
        self.assertEqual(first["existing_count"], 0)
        imported_task_id = first["imported"][0]["task"]["id"]
        imported_task = self.service.get_task_detail(imported_task_id)["task"]
        self.assertEqual(imported_task["external_ref"], "pg_1")
        self.assertEqual(imported_task["target"], "notion_task_sync_item")

        second = self.service.sync_notion_tasks(
            user_request="Sync Notion intake",
            classification=classification,
            statuses=["Inbox"],
            limit=10,
        )
        self.assertEqual(second["imported_count"], 0)
        self.assertEqual(second["existing_count"], 1)
        self.assertEqual(second["existing"][0]["match"], "external_ref")

    def test_sync_deduplicates_by_operation_key_and_links_external_ref(self) -> None:
        create_payload = self.service.create_request(
            user_request="Existing backend task",
            classification=RequestClassification(
                domain="technical",
                intent_type="capture",
                risk_level="low",
            ).validate(),
            target="daily_routine_followup",
            operation_key="op_existing",
            action_source="custom_adapter",
        )
        existing_task_id = create_payload["task"].id
        self.service._notion_adapter = _FakeNotionAdapter(
            {"Inbox": [_notion_task(page_id="pg_2", title="Existing task mirror", status="Inbox", operation_key="op_existing")]}
        )
        classification = RequestClassification(domain="system", intent_type="read", risk_level="low").validate()

        result = self.service.sync_notion_tasks(
            user_request="Sync Notion intake",
            classification=classification,
            statuses=["Inbox"],
            limit=10,
        )

        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(result["existing_count"], 1)
        self.assertEqual(result["existing"][0]["match"], "operation_key")
        self.assertEqual(result["existing"][0]["task"]["id"], existing_task_id)
        linked = self.service.get_task_detail(existing_task_id)["task"]
        self.assertEqual(linked["external_ref"], "pg_2")


if __name__ == "__main__":
    unittest.main()
