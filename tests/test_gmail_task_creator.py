from __future__ import annotations

import json
import shutil
from pathlib import Path

from src.agentic_os.config import Paths
from src.agentic_os.gmail_task_creator import create_tasks_from_inbox
from src.agentic_os.storage import Database


def _build_paths(tmp_path: Path) -> Paths:
    paths = Paths.from_root(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]
    shutil.copyfile(repo_root / "policy_rules.json", paths.policy_rules_path)
    return paths


def test_personal_inbox_creates_needs_approval_tasks_and_is_idempotent(tmp_path: Path) -> None:
    paths = _build_paths(tmp_path)
    inbox_summary = {
        "urgent": [
            {
                "subject": "Reply needed: legal sign-off",
                "sender": "counsel@example.com",
                "summary": "Need confirmation by tonight.",
                "actionable": True,
                "message_id": "msg-urgent-1",
            }
        ],
        "needs_reply": [
            {
                "subject": "Partnership intro follow-up",
                "sender": "partner@example.com",
                "summary": "Can we schedule next week?",
                "actionable": True,
                "message_id": "msg-reply-1",
            }
        ],
        "important_fyi": [
            {
                "subject": "FYI only",
                "sender": "team@example.com",
                "actionable": False,
                "message_id": "msg-fyi-1",
            }
        ],
    }

    created_once = create_tasks_from_inbox(inbox_summary, "personal", paths=paths)
    assert len(created_once) == 2
    created_twice = create_tasks_from_inbox(inbox_summary, "personal", paths=paths)
    assert created_twice == []

    db = Database(paths.db_path)
    task_urgent = db.get_task_by_operation_key("gmail:msg-urgent-1")
    task_reply = db.get_task_by_operation_key("gmail:msg-reply-1")
    assert task_urgent is not None
    assert task_reply is not None
    assert task_urgent.status == "awaiting_approval"
    assert task_urgent.approval_state == "pending"
    assert task_urgent.domain == "personal"
    assert task_urgent.intent_type == "draft"
    assert task_urgent.external_ref == "gmail:msg-urgent-1"
    assert task_urgent.action_source == "openclaw_tool"
    assert task_reply.external_ref == "gmail:msg-reply-1"


def test_agent_inbox_creates_auto_execute_tasks(tmp_path: Path) -> None:
    paths = _build_paths(tmp_path)
    inbox_summary = {
        "operational_items": [
            {
                "subject": "Queue triage needed",
                "sender": "ops@example.com",
                "summary": "Process 8 pending items.",
                "requested_action": "Triage queue",
                "actionable": True,
                "message_id": "msg-ops-1",
            }
        ],
        "alerts": [
            {
                "subject": "Error rate elevated",
                "sender": "alerts@example.com",
                "summary": "5xx spike in API cluster.",
                "actionable": True,
                "message_id": "msg-alert-1",
            }
        ],
    }

    created = create_tasks_from_inbox(inbox_summary, "agent", paths=paths)
    assert len(created) == 2

    db = Database(paths.db_path)
    ops_task = db.get_task_by_operation_key("gmail:msg-ops-1")
    alert_task = db.get_task_by_operation_key("gmail:msg-alert-1")
    assert ops_task is not None
    assert alert_task is not None
    assert ops_task.intent_type == "execute"
    assert ops_task.status == "in_progress"
    assert ops_task.approval_state == "not_needed"
    assert alert_task.intent_type == "capture"
    assert alert_task.status == "in_progress"
    assert alert_task.approval_state == "not_needed"


def test_task_creator_respects_skip_rules_and_max_tasks_per_run(tmp_path: Path) -> None:
    paths = _build_paths(tmp_path)
    routing_config = {
        "auto_draft_domains": [],
        "skip_senders": ["notifications@"],
        "always_needs_approval_senders": [],
        "max_tasks_per_run": 1,
    }
    (paths.root / "email_routing.json").write_text(
        json.dumps(routing_config, sort_keys=True),
        encoding="utf-8",
    )
    inbox_summary = {
        "urgent": [
            {
                "subject": "System notice",
                "sender": "notifications@example.com",
                "actionable": True,
                "message_id": "msg-skip-1",
            },
            {
                "subject": "Not actionable",
                "sender": "person@example.com",
                "actionable": False,
                "message_id": "msg-skip-2",
            },
            {
                "subject": "Needs response",
                "sender": "person@example.com",
                "actionable": True,
                "message_id": "msg-create-1",
            },
            {
                "subject": "Another response",
                "sender": "person@example.com",
                "actionable": True,
                "message_id": "msg-create-2",
            },
        ]
    }

    created = create_tasks_from_inbox(inbox_summary, "personal", paths=paths)
    assert created and len(created) == 1

    db = Database(paths.db_path)
    assert db.get_task_by_operation_key("gmail:msg-create-1") is not None
    assert db.get_task_by_operation_key("gmail:msg-create-2") is None
