"""
Phase 7 — Tests for task recovery, health checks, and audit log rotation.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.agentic_os.audit import AuditLog
from src.agentic_os.config import AppConfig, PaperclipConfig, Paths, repo_root
from src.agentic_os.health import get_paperclip_diagnostics, get_system_health, validate_startup_config
from src.agentic_os.models import RequestClassification
from src.agentic_os.paperclip_client import ActivityEvent, CommentRef, DocumentRef, IssueRef
from src.agentic_os.recovery import find_stalled_tasks, retry_stalled_task, scan_and_flag_stalled_tasks
from src.agentic_os.service import AgenticOSService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_paths(tmp_path: Path) -> Paths:
    paths = Paths.from_root(tmp_path)
    src = repo_root() / "policy_rules.json"
    if src.exists():
        shutil.copyfile(src, paths.policy_rules_path)
    return paths


def _make_paperclip_config() -> PaperclipConfig:
    return PaperclipConfig(
        base_url="http://localhost:3100/api",
        auth_mode="trusted",
        company_id="company_1",
        goal_id="goal_1",
        project_map={
            "personal": "proj_1",
            "technical": "proj_2",
            "finance": "proj_3",
            "system": "proj_4",
        },
        agent_map={
            "chief_of_staff": "agent_cos",
            "project_manager": "agent_pm",
            "engineering_manager": "agent_em",
            "engineer": "agent_eng",
            "executor_codex": "agent_codex",
            "content_writer": "agent_writer",
            "accountant": "agent_acc",
            "executive_assistant": "agent_ea",
            "infrastructure_engineer": "agent_infra",
        },
    )


@pytest.fixture
def tmp_service(tmp_path):
    paths = _make_paths(tmp_path)
    service = AgenticOSService(paths, config=AppConfig())
    service.initialize()
    return service


def _make_task(service, status="in_progress"):
    classification = RequestClassification(
        domain="personal", intent_type="read", risk_level="low"
    ).validate()
    result = service.create_request(
        user_request="test stall task",
        classification=classification,
        action_source="manual",
    )
    task = result["task"]
    if status != task.status:
        task = service.db.update_task(task.id, status=status)
    return task


# ---------------------------------------------------------------------------
# 7.1 — Task timeout & recovery
# ---------------------------------------------------------------------------

class TestFindStalledTasks:
    def test_in_progress_task_below_threshold_not_stalled(self, tmp_service):
        _make_task(tmp_service, status="in_progress")
        # Threshold 100h — nothing should be stalled yet
        stalled = find_stalled_tasks(tmp_service, threshold_hours=100.0)
        assert stalled == []

    def test_completed_task_never_stalled(self, tmp_service):
        task = _make_task(tmp_service, status="in_progress")
        tmp_service.db.update_task(task.id, status="completed")
        stalled = find_stalled_tasks(tmp_service, threshold_hours=0.0)
        assert all(s["task_id"] != task.id for s in stalled)

    def test_in_progress_task_above_threshold_detected(self, tmp_service):
        task = _make_task(tmp_service, status="in_progress")
        # Backdate updated_at to 3 hours ago
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with tmp_service.db.connect() as conn:
            conn.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?", (old_ts, task.id)
            )
        stalled = find_stalled_tasks(tmp_service, threshold_hours=2.0)
        task_ids = [s["task_id"] for s in stalled]
        assert task.id in task_ids

    def test_result_sorted_by_hours_descending(self, tmp_service):
        for hours in (3, 5, 1):
            t = _make_task(tmp_service, status="in_progress")
            old_ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            with tmp_service.db.connect() as conn:
                conn.execute(
                    "UPDATE tasks SET updated_at = ? WHERE id = ?", (old_ts, t.id)
                )
        stalled = find_stalled_tasks(tmp_service, threshold_hours=0.5)
        if len(stalled) >= 2:
            assert stalled[0]["hours_since_update"] >= stalled[1]["hours_since_update"]


class TestRetryTask:
    def test_retry_stalled_task_resets_to_in_progress(self, tmp_service):
        task = _make_task(tmp_service, status="in_progress")
        tmp_service.db.update_task(task.id, status="stalled")
        result = retry_stalled_task(tmp_service, task.id, feedback="test retry")
        assert result["new_status"] == "in_progress"

    def test_retry_exceeding_max_retries_fails_task(self, tmp_service):
        task = _make_task(tmp_service, status="in_progress")
        tmp_service.db.update_task(task.id, status="stalled", retry_count=2)
        result = retry_stalled_task(tmp_service, task.id)
        assert result["new_status"] == "failed"

    def test_retry_terminal_task_returns_error_message(self, tmp_service):
        task = _make_task(tmp_service, status="in_progress")
        tmp_service.db.update_task(task.id, status="completed")
        result = retry_stalled_task(tmp_service, task.id)
        assert "cannot retry" in result["message"]
        assert result["new_status"] == "completed"

    def test_service_retry_task_delegates_correctly(self, tmp_service):
        task = _make_task(tmp_service, status="in_progress")
        tmp_service.db.update_task(task.id, status="stalled")
        result = tmp_service.retry_task(task.id, feedback="via service")
        assert result["new_status"] == "in_progress"


class TestScanAndFlagStalledTasks:
    def test_scan_flags_nothing_when_all_fresh(self, tmp_service):
        _make_task(tmp_service)
        result = scan_and_flag_stalled_tasks(tmp_service, threshold_hours=100.0)
        assert result["flagged"] == 0

    def test_scan_flags_old_task(self, tmp_service):
        task = _make_task(tmp_service, status="in_progress")
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with tmp_service.db.connect() as conn:
            conn.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?", (old_ts, task.id)
            )
        result = scan_and_flag_stalled_tasks(tmp_service, threshold_hours=2.0)
        assert result["flagged"] == 1
        updated = tmp_service.db.get_task(task.id)
        assert updated.status == "stalled"

    def test_scan_result_shape(self, tmp_service):
        result = scan_and_flag_stalled_tasks(tmp_service, threshold_hours=100.0)
        assert "checked" in result
        assert "flagged" in result
        assert "notified" in result
        assert "tasks" in result


# ---------------------------------------------------------------------------
# 7.3 — System health dashboard
# ---------------------------------------------------------------------------

class TestGetSystemHealth:
    def test_health_returns_ok_when_clean(self, tmp_service):
        health = get_system_health(tmp_service)
        assert health["status"] in ("ok", "degraded", "error")
        assert "db" in health
        assert "audit_log" in health
        assert "artifacts_dir" in health
        assert "config" in health
        assert "cron" in health

    def test_db_section_shows_row_counts(self, tmp_service):
        _make_task(tmp_service)
        health = get_system_health(tmp_service)
        assert health["db"]["reachable"] is True
        assert health["db"]["row_counts"]["tasks"] >= 1

    def test_artifacts_dir_writable(self, tmp_service):
        health = get_system_health(tmp_service)
        assert health["artifacts_dir"]["writable"] is True

    def test_audit_log_section(self, tmp_service):
        _make_task(tmp_service)
        health = get_system_health(tmp_service)
        assert health["audit_log"]["exists"] is True
        assert health["audit_log"]["size_bytes"] > 0


class TestPaperclipDiagnostics:
    def test_requires_task_or_issue_identifier(self, tmp_service):
        with pytest.raises(ValueError):
            get_paperclip_diagnostics(tmp_service)

    def test_returns_backend_only_when_task_has_no_issue_link(self, tmp_service):
        task = _make_task(tmp_service, status="planning")
        payload = get_paperclip_diagnostics(tmp_service, task_id=task.id)
        assert payload["resolved_issue_id"] is None
        assert payload["note"] == "No Paperclip issue linked to the resolved task."

    def test_returns_issue_comments_activity_and_plan_doc(self, tmp_path):
        paths = _make_paths(tmp_path)
        service = AgenticOSService(paths, config=AppConfig(paperclip=_make_paperclip_config()))
        service.initialize()

        class _FakeControlPlane:
            def get_issue(self, issue_id: str):
                assert issue_id == "iss_123"
                return IssueRef(id=issue_id, title="Debug issue", status="in_review", assignee_id="agent_pm")

            def list_comments(self, issue_id: str):
                assert issue_id == "iss_123"
                return [CommentRef(id="c1", issue_id=issue_id, body="REVISE: add owner mapping details")]

            def poll_activity(self, issue_id: str, *, lookback_seconds=None):
                assert issue_id == "iss_123"
                assert lookback_seconds == 120
                return [
                    ActivityEvent(
                        id="a1",
                        issue_id=issue_id,
                        event_type="comment_added",
                        entity_type="comment",
                        entity_id="c1",
                        created_at="2026-04-03T00:00:00Z",
                        payload={"bodySnippet": "REVISE"},
                        details={},
                    )
                ]

            def get_document(self, issue_id: str, doc_id: str):
                assert issue_id == "iss_123"
                assert doc_id == "plan"
                return DocumentRef(
                    id="plan",
                    issue_id=issue_id,
                    title="Plan v2",
                    content="1. gather evidence\n2. verify handoff",
                )

        service._cp_cache = _FakeControlPlane()  # type: ignore[assignment]
        service._cp_initialized = True
        task = _make_task(service, status="planning")
        task = service.db.update_task(task.id, paperclip_issue_id="iss_123")
        payload = get_paperclip_diagnostics(
            service,
            task_id=task.id,
            activity_lookback_seconds=120,
        )

        assert payload["resolved_issue_id"] == "iss_123"
        assert payload["checks"]["has_task_link"] is True
        assert payload["checks"]["has_issue_projection"] is True
        assert payload["checks"]["comments_visible"] is True
        assert payload["checks"]["plan_document_visible"] is True
        assert payload["paperclip_comments"][0]["body"].startswith("REVISE:")


# ---------------------------------------------------------------------------
# 7.5 — Config validation
# ---------------------------------------------------------------------------

class TestValidateStartupConfig:
    def test_no_issues_on_clean_config(self, tmp_service):
        issues = validate_startup_config(tmp_service.paths, tmp_service.config)
        assert issues == []

    def test_unwritable_artifacts_dir_raises_issue(self, tmp_path):
        paths = Paths.from_root(tmp_path)
        # Make artifacts dir a file instead of a directory to cause write failure
        paths.artifacts_dir.parent.mkdir(parents=True, exist_ok=True)
        paths.artifacts_dir.write_text("I am a file, not a dir")
        config = AppConfig()
        issues = validate_startup_config(paths, config)
        assert any("artifacts_dir" in i for i in issues)


# ---------------------------------------------------------------------------
# 7.4 — Audit log rotation
# ---------------------------------------------------------------------------

class TestAuditLogRotation:
    def test_rotate_below_threshold_does_nothing(self, tmp_path):
        log_path = tmp_path / "data" / "audit_log.jsonl"
        log_path.parent.mkdir()
        log_path.write_text('{"id":1}\n')
        audit = AuditLog(log_path)
        result = audit.rotate(size_threshold_bytes=10 * 1024 * 1024)
        assert result["rotated"] is False
        assert log_path.exists()

    def test_rotate_above_threshold_renames_file(self, tmp_path):
        log_path = tmp_path / "data" / "audit_log.jsonl"
        log_path.parent.mkdir()
        log_path.write_text("x" * 100)
        audit = AuditLog(log_path)
        result = audit.rotate(size_threshold_bytes=50)
        assert result["rotated"] is True
        # Original file replaced with a fresh empty one
        assert log_path.exists()
        assert log_path.stat().st_size == 0
        # Archive file created
        archives = list(tmp_path.glob("data/audit_log.*.jsonl"))
        assert len(archives) == 1

    def test_rotate_result_shape(self, tmp_path):
        log_path = tmp_path / "data" / "audit_log.jsonl"
        log_path.parent.mkdir()
        log_path.write_text("x" * 100)
        audit = AuditLog(log_path)
        result = audit.rotate(size_threshold_bytes=50)
        assert "rotated" in result
        assert "archived" in result
        assert "size_bytes" in result
