"""Tests for execution_receiver (Phase 4.2)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.agentic_os.config import AppConfig, Paths, repo_root
from src.agentic_os.execution_receiver import (
    ExecutionParseError,
    ExecutionResult,
    _artifact_type_for_domain,
    receive_execution_result,
)
from src.agentic_os.models import RequestClassification
from src.agentic_os.paperclip_client import IssueRef
from src.agentic_os.service import AgenticOSService
from src.agentic_os.task_control_plane import TaskControlPlane


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_paths(tmp_path) -> Paths:
    paths = Paths.from_root(tmp_path)
    # Copy policy_rules.json so evaluate_policy doesn't fail
    src = repo_root() / "policy_rules.json"
    if src.exists():
        shutil.copyfile(src, paths.policy_rules_path)
    return paths


def _make_service(paths: Paths) -> AgenticOSService:
    svc = AgenticOSService(paths, AppConfig())
    svc.initialize()
    return svc


def _write_paperclip_config(paths: Paths) -> None:
    payload = {
        "paperclip": {
            "base_url": "http://127.0.0.1:65535/api",
            "auth_mode": "trusted",
            "company_id": "company_test",
            "goal_id": "goal_test",
            "project_map": {
                "personal": "proj_personal",
                "technical": "proj_technical",
                "finance": "proj_finance",
                "system": "proj_system",
            },
            "agent_map": {
                "chief_of_staff": "agent_cos",
                "project_manager": "agent_pm",
                "engineering_manager": "agent_em",
                "engineer": "agent_eng",
                "infrastructure_engineer": "agent_infra",
                "executor_codex": "agent_codex",
                "content_writer": "agent_writer",
                "accountant": "agent_acct",
                "executive_assistant": "agent_ea",
            },
        }
    }
    paths.config_path.write_text(json.dumps(payload), encoding="utf-8")


def _make_output(task_id: str, content: str) -> str:
    return (
        "Preamble text\n"
        "RESULT_START\n"
        f"{content}\n"
        "RESULT_END\n"
        f"TASK_DONE: {task_id}\n"
    )


# ---------------------------------------------------------------------------
# _artifact_type_for_domain
# ---------------------------------------------------------------------------

class TestArtifactTypeForDomain:
    def test_personal_draft_returns_email_draft_type(self):
        result = _artifact_type_for_domain("personal", "draft")
        assert result != "output"
        assert "draft" in result or "email" in result

    def test_technical_execute(self):
        assert _artifact_type_for_domain("technical", "execute") == "code"

    def test_technical_read(self):
        assert _artifact_type_for_domain("technical", "read") == "research_summary"

    def test_default(self):
        assert _artifact_type_for_domain("finance", "capture") == "output"
        assert _artifact_type_for_domain("system", "recap") == "output"


# ---------------------------------------------------------------------------
# receive_execution_result — parse errors
# ---------------------------------------------------------------------------

class TestReceiveExecutionResultParseErrors:
    def test_missing_both_markers_raises(self, tmp_path):
        paths = _make_paths(tmp_path)
        with pytest.raises(ExecutionParseError, match="RESULT_START"):
            receive_execution_result(
                "no markers here",
                task_id="task_abc",
                session_key="sess_001",
                paths=paths,
            )

    def test_missing_result_end_raises(self, tmp_path):
        paths = _make_paths(tmp_path)
        with pytest.raises(ExecutionParseError):
            receive_execution_result(
                "RESULT_START\nsome content",
                task_id="task_abc",
                session_key="sess_001",
                paths=paths,
            )

    def test_missing_task_done_raises(self, tmp_path):
        paths = _make_paths(tmp_path)
        with pytest.raises(ExecutionParseError, match="TASK_DONE"):
            receive_execution_result(
                "RESULT_START\nsome content\nRESULT_END",
                task_id="task_abc",
                session_key="sess_001",
                paths=paths,
            )

    def test_mismatched_task_done_raises(self, tmp_path):
        paths = _make_paths(tmp_path)
        with pytest.raises(ExecutionParseError, match="TASK_DONE"):
            receive_execution_result(
                "RESULT_START\ncontent\nRESULT_END\nTASK_DONE: other_task",
                task_id="task_abc",
                session_key="sess_001",
                paths=paths,
            )


# ---------------------------------------------------------------------------
# receive_execution_result — happy path
# ---------------------------------------------------------------------------

class TestReceiveExecutionResultSuccess:
    def _create_task(self, svc: AgenticOSService, domain: str = "technical", intent: str = "read") -> str:
        from uuid import uuid4
        classification = RequestClassification(
            domain=domain,
            intent_type=intent,
            risk_level="low",
        )
        # execute/draft policies may require operation_key; always provide one
        result = svc.create_request(
            user_request="Write hello world script",
            classification=classification,
            operation_key=f"op_{uuid4().hex[:8]}",
        )
        return result["task"].id

    def test_returns_execution_result(self, tmp_path):
        paths = _make_paths(tmp_path)
        svc = _make_service(paths)
        task_id = self._create_task(svc)

        raw = _make_output(task_id, "print('hello world')")
        result = receive_execution_result(
            raw,
            task_id=task_id,
            session_key="sess_xyz",
            paths=paths,
        )

        assert isinstance(result, ExecutionResult)
        assert result.success is True
        assert result.task_id == task_id
        assert result.artifact_id != ""
        assert result.error is None

    def test_artifact_file_written(self, tmp_path):
        paths = _make_paths(tmp_path)
        svc = _make_service(paths)
        task_id = self._create_task(svc)

        raw = _make_output(task_id, "def foo(): pass")
        result = receive_execution_result(
            raw,
            task_id=task_id,
            session_key="sess_xyz",
            paths=paths,
        )

        assert result.success
        artifact_dir = paths.artifacts_dir / task_id
        assert artifact_dir.exists()
        files = list(artifact_dir.iterdir())
        assert len(files) == 1
        assert result.artifact_id in files[0].name

    def test_task_marked_executed_or_completed(self, tmp_path):
        paths = _make_paths(tmp_path)
        svc = _make_service(paths)
        task_id = self._create_task(svc)

        raw = _make_output(task_id, "result content")
        receive_execution_result(
            raw,
            task_id=task_id,
            session_key="sess_xyz",
            paths=paths,
        )

        # Reload task from DB
        svc2 = _make_service(paths)
        task = svc2.db.get_task(task_id)
        assert task.status in {"executed", "completed"}

    def test_result_content_extracted(self, tmp_path):
        paths = _make_paths(tmp_path)
        svc = _make_service(paths)
        task_id = self._create_task(svc)

        content = "The analysis shows X, Y, and Z."
        raw = _make_output(task_id, content)
        receive_execution_result(
            raw,
            task_id=task_id,
            session_key="sess_xyz",
            paths=paths,
        )

        svc2 = _make_service(paths)
        task = svc2.db.get_task(task_id)
        assert content[:50] in (task.result_summary or "")

    def test_personal_draft_uses_draft_artifact_type(self, tmp_path):
        paths = _make_paths(tmp_path)
        svc = _make_service(paths)
        task_id = self._create_task(svc, domain="personal", intent="draft")

        raw = _make_output(task_id, "Dear Dara, ...")
        result = receive_execution_result(
            raw,
            task_id=task_id,
            session_key="sess_xyz",
            paths=paths,
        )
        assert result.success

    def test_resolves_task_by_paperclip_issue_id(self, tmp_path):
        paths = _make_paths(tmp_path)
        svc = _make_service(paths)
        backend_task_id = self._create_task(svc)
        issue_id = "56439d65-edb9-4844-b29c-ec2729f356e5"
        svc.db.update_task(backend_task_id, paperclip_issue_id=issue_id)

        # Simulate a callback that passes Paperclip issue ID as task_id.
        raw = _make_output(issue_id, "resolved via issue mapping")
        result = receive_execution_result(
            raw,
            task_id=issue_id,
            session_key="sess_xyz",
            paths=paths,
        )

        assert result.success is True
        assert result.task_id == backend_task_id

    def test_resolves_task_by_paperclip_routine_run_id(self, tmp_path):
        paths = _make_paths(tmp_path)
        svc = _make_service(paths)
        backend_task_id = self._create_task(svc)
        run_id = "f3f2a81a-dad1-47c0-9d73-f451a1fdf9be"
        svc.db.update_task(backend_task_id, paperclip_routine_run_id=run_id)

        # Simulate a callback that passes routine run ID as task_id.
        raw = _make_output(run_id, "resolved via run mapping")
        result = receive_execution_result(
            raw,
            task_id=run_id,
            session_key="sess_xyz",
            paths=paths,
        )

        assert result.success is True
        assert result.task_id == backend_task_id

    def test_imports_paperclip_issue_when_callback_arrives_before_backend_import(
        self,
        tmp_path,
        monkeypatch,
    ):
        paths = _make_paths(tmp_path)
        _write_paperclip_config(paths)
        _make_service(paths)

        issue_id = "d8f51b64-f0d8-4adc-9c0f-17cdb7a1ab46"
        run_id = "bf7f0a8f-30d7-43fb-9261-cb89bf207cb6"
        session_key = f"paperclip:run:{run_id}"

        monkeypatch.setattr(
            TaskControlPlane,
            "get_issue",
            lambda self, _: IssueRef(  # noqa: ARG005
                id=issue_id,
                title="Daily Recap",
                status="todo",
                project_id="proj_system",
            ),
        )
        monkeypatch.setattr(
            TaskControlPlane,
            "adopt_issue",
            lambda self, issue_id, task, assignee_key=None: IssueRef(  # noqa: ARG005
                id=issue_id,
                title=task.title or "Imported",
                status="todo",
                project_id="proj_system",
            ),
        )
        monkeypatch.setattr(TaskControlPlane, "_write_import_brief", lambda self, issue_id, task: None)

        raw = _make_output(issue_id, "Recovered callback after import race")
        result = receive_execution_result(
            raw,
            task_id=issue_id,
            session_key=session_key,
            paths=paths,
        )

        assert result.success is True
        assert result.task_id.startswith("task_")
        assert result.task_id != issue_id

        svc = _make_service(paths)
        imported_task = svc.db.get_task(result.task_id)
        assert imported_task.paperclip_issue_id == issue_id
        assert imported_task.paperclip_routine_run_id == run_id
        assert imported_task.paperclip_origin_kind == "routine_execution"
        assert imported_task.status in {"completed", "executed"}

    def test_returns_error_on_db_not_found(self, tmp_path):
        paths = _make_paths(tmp_path)
        # Initialize DB so it exists but task doesn't
        svc = _make_service(paths)

        raw = _make_output("nonexistent_task", "content")
        result = receive_execution_result(
            raw,
            task_id="nonexistent_task",
            session_key="sess_xyz",
            paths=paths,
        )
        # Should return error result, not raise
        assert result.success is False
        assert result.error is not None
