from __future__ import annotations

import json

from src.agentic_os.email_draft_handler import (
    EMAIL_DRAFT_ARTIFACT_TYPE,
    build_email_draft_brief,
    build_email_draft_brief_for_task,
)
from src.agentic_os.models import TaskRecord


def _task_record(*, task_id: str, user_request: str, request_metadata_json: str | None) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        created_at="2026-03-22T16:00:00Z",
        updated_at="2026-03-22T16:00:00Z",
        domain="personal",
        intent_type="draft",
        risk_level="medium",
        status="awaiting_approval",
        approval_state="pending",
        user_request=user_request,
        result_summary=None,
        artifact_ref=None,
        external_ref="gmail:msg-1",
        target="gmail_reply_draft",
        request_metadata_json=request_metadata_json,
        operation_key="gmail:msg-1",
        external_write=False,
        policy_decision="approval_required",
        action_source="openclaw_tool",
        retry_count=0,
    )


def test_build_email_draft_brief_contains_required_markers() -> None:
    brief = build_email_draft_brief(
        task_id="task_000123",
        subject="Contract update",
        sender="legal@example.com",
        summary="Need response before EOD.",
    )
    assert "You are drafting an email reply on behalf of Dara." in brief
    assert "Subject: Contract update" in brief
    assert "From: legal@example.com" in brief
    assert "RESULT_START" in brief
    assert "RESULT_END" in brief
    assert "TASK_DONE: task_000123" in brief


def test_build_email_draft_brief_for_task_uses_task_metadata() -> None:
    metadata = {
        "subject": "Partnership terms",
        "sender": "ops@example.com",
        "summary": "Please confirm the revised scope.",
    }
    task = _task_record(
        task_id="task_000124",
        user_request="Reply to: Partnership terms (from ops@example.com)",
        request_metadata_json=json.dumps(metadata, sort_keys=True),
    )
    brief = build_email_draft_brief_for_task(task)
    assert "Subject: Partnership terms" in brief
    assert "From: ops@example.com" in brief
    assert "Summary: Please confirm the revised scope." in brief
    assert "TASK_DONE: task_000124" in brief
    assert EMAIL_DRAFT_ARTIFACT_TYPE == "email_draft"
