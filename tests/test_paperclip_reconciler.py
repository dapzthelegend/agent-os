"""Tests for PaperclipReconciler (Phase 4)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.agentic_os.config import AppConfig, Paths, repo_root
from src.agentic_os.models import RequestClassification
from src.agentic_os.paperclip_client import ActivityEvent
from src.agentic_os.paperclip_reconciler import PaperclipReconciler
from src.agentic_os.service import AgenticOSService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_paths(tmp_path: Path) -> Paths:
    paths = Paths.from_root(tmp_path)
    src = repo_root() / "policy_rules.json"
    if src.exists():
        shutil.copyfile(src, paths.policy_rules_path)
    return paths


def _make_service(paths: Paths) -> AgenticOSService:
    svc = AgenticOSService(paths, AppConfig())
    svc.initialize()
    return svc


def _make_reconciler(tmp_path: Path) -> tuple[PaperclipReconciler, AgenticOSService]:
    paths = _make_paths(tmp_path)
    config = AppConfig()
    svc = _make_service(paths)
    r = PaperclipReconciler(paths, config)
    # Inject the already-initialized service so tests share the same DB
    r._service = svc
    return r, svc


def _activity_event(
    event_id: str,
    issue_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    entity_type: str = "",
    entity_id: str = "",
    run_id: str | None = None,
) -> ActivityEvent:
    return ActivityEvent(
        id=event_id,
        issue_id=issue_id,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        run_id=run_id,
        actor="operator",
        payload=payload,
        details={},
        created_at="2026-01-01T00:00:00Z",
    )


def _make_plan_first_task(svc: AgenticOSService) -> Any:
    """Create a plan_first task in awaiting_plan_review with a fake paperclip_issue_id."""
    classification = RequestClassification(
        domain="technical",
        intent_type="content",
        risk_level="low",
        status="new",
        approval_state="not_needed",
    )
    task = svc.db.create_task(
        classification=classification,
        user_request="Write a technical specification for auth redesign",
        policy_decision="read_ok",
    )
    task = svc.db.update_task(
        task.id,
        status="awaiting_plan_review",
        task_mode="plan_first",
        paperclip_issue_id="issue-abc",
        plan_version=1,
    )
    return task


# ---------------------------------------------------------------------------
# Seen-set persistence
# ---------------------------------------------------------------------------

class TestSeenPersistence:
    def test_load_seen_returns_empty_set_when_no_file(self, tmp_path):
        r, _ = _make_reconciler(tmp_path)
        assert r._load_seen() == set()

    def test_save_and_reload(self, tmp_path):
        r, _ = _make_reconciler(tmp_path)
        r._save_seen({"evt-1", "evt-2"})
        loaded = r._load_seen()
        assert loaded == {"evt-1", "evt-2"}

    def test_trimming_to_max_seen(self, tmp_path):
        from src.agentic_os.paperclip_reconciler import _MAX_SEEN
        r, _ = _make_reconciler(tmp_path)
        big_set = {f"evt-{i}" for i in range(_MAX_SEEN + 100)}
        r._save_seen(big_set)
        loaded = r._load_seen()
        assert len(loaded) <= _MAX_SEEN

    def test_corrupted_state_file_returns_empty_set(self, tmp_path):
        r, _ = _make_reconciler(tmp_path)
        r._state_path.parent.mkdir(parents=True, exist_ok=True)
        r._state_path.write_text("not json", encoding="utf-8")
        assert r._load_seen() == set()


# ---------------------------------------------------------------------------
# run_once with no Paperclip config
# ---------------------------------------------------------------------------

class TestRunOnceNoPaperclip:
    def test_returns_not_configured_note(self, tmp_path):
        r, _ = _make_reconciler(tmp_path)
        result = r.run_once()
        assert result["note"] == "paperclip not configured"
        assert result["events_polled"] == 0


# ---------------------------------------------------------------------------
# _dispatch: unknown issue
# ---------------------------------------------------------------------------

class TestDispatchUnknownIssue:
    def test_returns_none_for_unknown_issue(self, tmp_path):
        r, _ = _make_reconciler(tmp_path)
        event = _activity_event("e1", "no-such-issue", "comment_added", {"body": "APPROVE"})
        result = r._dispatch(event)
        assert result is None


# ---------------------------------------------------------------------------
# Comment handling — plan approval
# ---------------------------------------------------------------------------

class TestCommentApproval:
    @pytest.mark.parametrize("body", ["APPROVE", "approve", "lgtm", "LGTM", "approved", "Approved"])
    def test_approval_signal_approves_plan(self, tmp_path, body):
        r, svc = _make_reconciler(tmp_path)
        task = _make_plan_first_task(svc)

        event = _activity_event("e1", "issue-abc", "comment_added", {"body": body})
        action = r._dispatch(event)

        assert action == "plan_approved"
        updated = svc.db.get_task(task.id)
        assert updated.status == "approved_for_execution"
        assert updated.approved_plan_revision_id == "plan-v1-reconciler"

    def test_approval_ignored_when_not_in_awaiting_review(self, tmp_path):
        r, svc = _make_reconciler(tmp_path)
        task = _make_plan_first_task(svc)
        svc.db.update_task(task.id, status="executing")

        event = _activity_event("e1", "issue-abc", "comment_added", {"body": "APPROVE"})
        action = r._dispatch(event)

        assert action is None
        updated = svc.db.get_task(task.id)
        assert updated.status == "executing"


# ---------------------------------------------------------------------------
# Comment handling — plan revision
# ---------------------------------------------------------------------------

class TestCommentRevision:
    @pytest.mark.parametrize("body", [
        "REVISE: please add error handling",
        "REVISION: scope is too broad",
        "REQUEST_REVISION: needs auth section",
    ])
    def test_revision_signal_rejects_plan(self, tmp_path, body):
        r, svc = _make_reconciler(tmp_path)
        task = _make_plan_first_task(svc)

        event = _activity_event("e1", "issue-abc", "comment_added", {"body": body})
        action = r._dispatch(event)

        assert action == "plan_rejected"
        updated = svc.db.get_task(task.id)
        assert updated.status == "planning"

    def test_revision_ignored_when_not_in_awaiting_review(self, tmp_path):
        r, svc = _make_reconciler(tmp_path)
        task = _make_plan_first_task(svc)
        svc.db.update_task(task.id, status="planning")

        event = _activity_event("e1", "issue-abc", "comment_added", {"body": "REVISE: something"})
        action = r._dispatch(event)

        assert action is None


# ---------------------------------------------------------------------------
# Comment handling — ambiguous
# ---------------------------------------------------------------------------

class TestCommentAmbiguous:
    def test_ambiguous_comment_returns_none(self, tmp_path):
        r, svc = _make_reconciler(tmp_path)
        task = _make_plan_first_task(svc)

        event = _activity_event("e1", "issue-abc", "comment_added", {"body": "Looks interesting"})
        action = r._dispatch(event)
        assert action is None

        # Task untouched
        updated = svc.db.get_task(task.id)
        assert updated.status == "awaiting_plan_review"


# ---------------------------------------------------------------------------
# Status change handling — cancellation
# ---------------------------------------------------------------------------

class TestStatusChangeCancelled:
    def test_cancelled_status_cancels_task(self, tmp_path):
        r, svc = _make_reconciler(tmp_path)
        task = _make_plan_first_task(svc)

        event = _activity_event("e1", "issue-abc", "status_changed", {"status": "cancelled"})
        action = r._dispatch(event)

        assert action == "task_cancelled"
        updated = svc.db.get_task(task.id)
        assert updated.status == "cancelled"

    def test_already_cancelled_is_noop(self, tmp_path):
        r, svc = _make_reconciler(tmp_path)
        task = _make_plan_first_task(svc)
        svc.db.update_task(task.id, status="cancelled")

        event = _activity_event("e1", "issue-abc", "status_changed", {"status": "cancelled"})
        action = r._dispatch(event)
        assert action is None

    def test_already_completed_is_noop(self, tmp_path):
        r, svc = _make_reconciler(tmp_path)
        task = _make_plan_first_task(svc)
        svc.db.update_task(task.id, status="completed")

        event = _activity_event("e1", "issue-abc", "status_changed", {"status": "cancelled"})
        action = r._dispatch(event)
        assert action is None

    def test_non_cancelled_status_is_ignored(self, tmp_path):
        r, svc = _make_reconciler(tmp_path)
        task = _make_plan_first_task(svc)

        event = _activity_event("e1", "issue-abc", "status_changed", {"status": "in_progress"})
        action = r._dispatch(event)
        assert action is None


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_seen_event_is_skipped_in_run_once(self, tmp_path):
        r, svc = _make_reconciler(tmp_path)
        task = _make_plan_first_task(svc)

        # Pre-populate seen set with this event ID
        r._save_seen({"e1"})

        approval_event = _activity_event("e1", "issue-abc", "comment_added", {"body": "APPROVE"})

        # Inject a fake cp that returns this event
        mock_cp = MagicMock()
        mock_cp.poll_company_activity.return_value = [approval_event]
        r._cp = mock_cp

        result = r.run_once()

        assert result["skipped"] == 1
        assert result["actions_taken"] == 0
        # Task must not have changed
        updated = svc.db.get_task(task.id)
        assert updated.status == "awaiting_plan_review"

    def test_event_not_reprocessed_on_second_run(self, tmp_path):
        r, svc = _make_reconciler(tmp_path)
        task = _make_plan_first_task(svc)

        approval_event = _activity_event("e1", "issue-abc", "comment_added", {"body": "APPROVE"})

        mock_cp = MagicMock()
        mock_cp.poll_company_activity.return_value = [approval_event]
        r._cp = mock_cp

        # First run — should take action
        result1 = r.run_once()
        assert result1["actions_taken"] == 1

        # Reset task to awaiting_plan_review to prove idempotency (not re-approved)
        svc.db.update_task(task.id, status="awaiting_plan_review", approved_plan_revision_id=None)

        # Second run — same event, should be skipped
        result2 = r.run_once()
        assert result2["skipped"] == 1
        assert result2["actions_taken"] == 0
        # Task should still be awaiting_plan_review (not re-approved)
        updated = svc.db.get_task(task.id)
        assert updated.status == "awaiting_plan_review"


# ---------------------------------------------------------------------------
# run_once summary counts
# ---------------------------------------------------------------------------

class TestRunOnceSummary:
    def test_run_once_returns_correct_counts(self, tmp_path):
        r, svc = _make_reconciler(tmp_path)
        task = _make_plan_first_task(svc)

        events = [
            _activity_event("e1", "issue-abc", "comment_added", {"body": "APPROVE"}),
            _activity_event("e2", "issue-abc", "comment_added", {"body": "Nice work"}),
            _activity_event("e3", "no-such-issue", "comment_added", {"body": "APPROVE"}),
        ]

        mock_cp = MagicMock()
        mock_cp.poll_company_activity.return_value = events
        r._cp = mock_cp

        result = r.run_once()

        assert result["events_polled"] == 3
        assert result["actions_taken"] == 1   # only e1 triggers plan_approved
        assert result["errors"] == 0

    def test_poll_failure_returns_error_count(self, tmp_path):
        r, svc = _make_reconciler(tmp_path)

        mock_cp = MagicMock()
        mock_cp.poll_company_activity.side_effect = RuntimeError("network error")
        r._cp = mock_cp

        result = r.run_once()
        assert result["errors"] == 1
        assert result["events_polled"] == 0


class TestRoutineRunEvents:
    def test_coalesced_routine_run_does_not_create_task(self, tmp_path):
        r, svc = _make_reconciler(tmp_path)
        before = len(svc.db.list_tasks(limit=100))

        event = _activity_event(
            "e1",
            "",
            "routine.run_triggered",
            {"status": "coalesced", "routineId": "routine-1"},
            entity_type="routine_run",
            entity_id="run-1",
            run_id="run-1",
        )
        action = r._dispatch(event)

        assert action == "routine_run_non_task_terminal"
        after = len(svc.db.list_tasks(limit=100))
        assert after == before

    def test_routine_run_links_to_existing_issue_task(self, tmp_path):
        r, svc = _make_reconciler(tmp_path)
        task = _make_plan_first_task(svc)

        event = _activity_event(
            "e2",
            "issue-abc",
            "routine.run_triggered",
            {
                "status": "issue_created",
                "routineId": "routine-xyz",
                "linkedIssueId": "issue-abc",
            },
            entity_type="routine_run",
            entity_id="run-xyz",
            run_id="run-xyz",
        )
        action = r._dispatch(event)

        assert action == "routine_run_linked"
        updated = svc.db.get_task(task.id)
        assert updated.paperclip_routine_id == "routine-xyz"
        assert updated.paperclip_routine_run_id == "run-xyz"
        assert updated.paperclip_origin_kind == "routine_execution"
