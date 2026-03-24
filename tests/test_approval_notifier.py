from __future__ import annotations

from src.agentic_os.approval_notifier import build_approval_email
from src.agentic_os.models import TaskRecord


def _task_record() -> TaskRecord:
    return TaskRecord(
        id="task_000777",
        created_at="2026-03-22T16:30:00Z",
        updated_at="2026-03-22T16:30:00Z",
        domain="personal",
        intent_type="draft",
        risk_level="medium",
        status="awaiting_approval",
        approval_state="pending",
        user_request="Reply to: Partnership terms (from ops@example.com)\nSummary: confirm scope",
        result_summary=None,
        artifact_ref=None,
        external_ref="gmail:msg-777",
        target="gmail_reply_draft",
        request_metadata_json=None,
        operation_key="gmail:msg-777",
        external_write=False,
        policy_decision="approval_required",
        action_source="openclaw_tool",
        retry_count=0,
    )


def test_build_approval_email_includes_draft_block_and_truncates() -> None:
    task = _task_record()
    draft = "x" * 1205
    payload = build_approval_email(task, draft)
    assert payload["to"] == "franchieinc@gmail.com"
    assert payload["subject"] == "[Agent] Approval needed: Reply to: Partnership terms (from ops@example.com)"
    assert "Task: Reply to: Partnership terms (from ops@example.com)" in payload["body"]
    assert "Domain: personal | Risk: medium" in payload["body"]
    assert "Draft:" in payload["body"]
    assert ("x" * 1000) in payload["body"]
    assert ("x" * 1001) not in payload["body"]
    assert 'To approve: reply "approve task_000777"' in payload["body"]


def test_build_approval_email_without_artifact_omits_draft_block() -> None:
    task = _task_record()
    payload = build_approval_email(task, None)
    assert "Draft:" not in payload["body"]
    assert 'To reject:  reply "reject task_000777 <reason>"' in payload["body"]
