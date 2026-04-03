"""
DEPRECATED (Phase 6 — Paperclip cutover).

Notion result writeback — updates Notion after task completion or failure.

This module is no longer part of the live execution path. Completion and failure
writebacks now go to Paperclip exclusively (via TaskControlPlane inside service.py).
The fail_task() Notion writeback was removed in Phase 6; execution_receiver.py
Notion writeback was removed in Phase 3.
Retained for rollback capability only — do not import from live code.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from .config import AppConfig
from .models import TaskRecord
from .notion import NotionAdapter, _request_with_retry


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _word_count(text: str) -> int:
    return len(text.split())


def write_success(
    page_id: str,
    task: TaskRecord,
    result_summary: str,
    *,
    config: AppConfig,
    artifact_id: Optional[str] = None,
) -> None:
    """
    Update Notion page to Done, write full result body, and set Last Agent Update timestamp.

    Writes:
    - Status property → Done
    - Last Agent Update property → current UTC ISO timestamp
    - Page body → divider + result heading + full result text + artifact reference
    """
    adapter = NotionAdapter.for_db(config, "tasks")
    last_update = _utc_now_iso()

    # Update status and Last Agent Update property together
    adapter.update_task_status(
        page_id=page_id,
        status="Done",
        last_agent_update=last_update,
    )

    # Word count annotation for content tasks
    word_count: Optional[int] = None
    if task.intent_type == "content":
        word_count = _word_count(result_summary)

    # Build the full result body blocks
    blocks = _build_result_blocks(
        result_summary=result_summary,
        artifact_id=artifact_id,
        task_id=task.id,
        completed_at=last_update,
        word_count=word_count,
    )

    # Append to page body
    _append_blocks(page_id=page_id, blocks=blocks, config=config)


def write_content_page(
    page_id: str,
    task: TaskRecord,
    content: str,
    *,
    config: AppConfig,
) -> Optional[str]:
    """
    For personal/draft tasks: create a Notion child page with full content.
    Also updates the parent page status to Done with Last Agent Update timestamp.

    Returns the new child page ID on success, or None if creation fails.
    """
    notion_cfg = config.get_notion_db("tasks")
    token = notion_cfg.require_api_token()
    title = (task.user_request.splitlines()[0].strip() or "Draft")[:100]
    last_update = _utc_now_iso()

    # Build content blocks for the child page
    children = []
    for chunk in _chunk_text(content, 1800):
        children.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": chunk}}]
                },
            }
        )

    payload = {
        "parent": {"page_id": page_id},
        "properties": {
            "title": {
                "title": [{"type": "text", "text": {"content": title}}]
            }
        },
        "children": children,
    }

    from urllib import request as urllib_request

    req = urllib_request.Request(
        f"{notion_cfg.api_base_url}/pages",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": notion_cfg.notion_version,
        },
        method="POST",
    )
    try:
        result = _request_with_retry(req)
        child_page_id = result.get("id")

        # Update parent page: Done status + Last Agent Update + link to child page
        adapter = NotionAdapter.for_db(config, "tasks")
        adapter.update_task_status(
            page_id=page_id,
            status="Done",
            last_agent_update=last_update,
        )
        if child_page_id:
            adapter.append_note(
                page_id=page_id,
                note=f"✅ Done — full content written to child page (task {task.id})",
            )
        return child_page_id
    except Exception:
        return None


def write_failure(page_id: str, reason: str, *, config: AppConfig) -> None:
    """
    Update Notion page to Blocked, write failure detail to page body,
    and set Last Agent Update timestamp.
    """
    adapter = NotionAdapter.for_db(config, "tasks")
    last_update = _utc_now_iso()

    adapter.update_task_status(
        page_id=page_id,
        status="Blocked",
        last_agent_update=last_update,
    )

    # Append failure detail blocks to page body
    blocks = [
        {
            "object": "block",
            "type": "divider",
            "divider": {},
        },
        {
            "object": "block",
            "type": "heading_3",
            "heading_3": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": f"❌ Agent Failure — {last_update}"},
                    }
                ]
            },
        },
    ]
    for chunk in _chunk_text(reason[:2000], 1800):
        blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": chunk},
                            "annotations": {"color": "red"},
                        }
                    ]
                },
            }
        )
    _append_blocks(page_id=page_id, blocks=blocks, config=config)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_result_blocks(
    result_summary: str,
    artifact_id: Optional[str],
    task_id: str,
    completed_at: str,
    word_count: Optional[int] = None,
) -> list[dict]:
    """Build Notion blocks for the result section written to a page body."""
    heading_text = f"✅ Agent Result — {completed_at}"
    if word_count is not None:
        heading_text += f" ({word_count:,} words)"

    blocks: list[dict] = [
        {
            "object": "block",
            "type": "divider",
            "divider": {},
        },
        {
            "object": "block",
            "type": "heading_3",
            "heading_3": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": heading_text},
                    }
                ]
            },
        },
    ]

    # Full result summary (chunked)
    for chunk in _chunk_text(result_summary, 1800):
        blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": chunk}}]
                },
            }
        )

    # Artifact reference
    if artifact_id:
        meta_parts = [f"Artifact: {artifact_id}", f"Task: {task_id}"]
        if word_count is not None:
            meta_parts.append(f"Words: {word_count:,}")
        blocks.append(
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": "  |  ".join(meta_parts)},
                            "annotations": {"code": True},
                        }
                    ]
                },
            }
        )

    return blocks


def _append_blocks(page_id: str, blocks: list[dict], *, config: AppConfig) -> None:
    """Append a list of blocks to a Notion page body."""
    notion_cfg = config.get_notion_db("tasks")
    token = notion_cfg.require_api_token()

    from urllib import request as urllib_request

    payload = {"children": blocks}
    req = urllib_request.Request(
        f"{notion_cfg.api_base_url}/blocks/{page_id}/children",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": notion_cfg.notion_version,
        },
        method="PATCH",
    )
    _request_with_retry(req)


def _chunk_text(text: str, max_len: int) -> list[str]:
    chunks = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks
