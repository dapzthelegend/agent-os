"""Tests for Phase 5.2 — idempotency enforcement in execution_receiver and service."""
from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from src.agentic_os.config import AppConfig, Paths, repo_root
from src.agentic_os.execution_receiver import ExecutionResult, receive_execution_result
from src.agentic_os.models import RequestClassification
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


def _make_task(svc: AgenticOSService, operation_key: str) -> str:
    classification = RequestClassification(
        domain="technical",
        intent_type="read",
        risk_level="low",
    )
    result = svc.create_request(
        user_request="Analyse the logs",
        classification=classification,
        operation_key=operation_key,
    )
    return result["task"].id


def _make_output(task_id: str, content: str = "analysis complete") -> str:
    return (
        "Preamble\n"
        "RESULT_START\n"
        f"{content}\n"
        "RESULT_END\n"
        f"TASK_DONE: {task_id}\n"
    )


# ---------------------------------------------------------------------------
# execution_receiver: duplicate operation_key returns idempotent result
# ---------------------------------------------------------------------------

class TestExecutionReceiverIdempotency:
    def test_second_submission_returns_idempotent_result(self, tmp_path):
        paths = _make_paths(tmp_path)
        svc = _make_service(paths)
        op_key = f"op_{uuid4().hex[:8]}"
        task_id = _make_task(svc, op_key)
        raw = _make_output(task_id)

        # First submission — should succeed normally
        first = receive_execution_result(raw, task_id=task_id, session_key="sess_001", paths=paths)
        assert first.success is True
        assert first.idempotent is False

        # Second submission with same operation_key — should return idempotent result
        second = receive_execution_result(raw, task_id=task_id, session_key="sess_002", paths=paths)
        assert second.success is True
        assert second.idempotent is True
        assert second.error is None

    def test_idempotent_result_does_not_create_second_artifact(self, tmp_path):
        paths = _make_paths(tmp_path)
        svc = _make_service(paths)
        op_key = f"op_{uuid4().hex[:8]}"
        task_id = _make_task(svc, op_key)
        raw = _make_output(task_id)

        receive_execution_result(raw, task_id=task_id, session_key="sess_001", paths=paths)
        receive_execution_result(raw, task_id=task_id, session_key="sess_002", paths=paths)

        # Only one artifact file should exist on disk (second call is idempotent)
        artifact_dir = paths.artifacts_dir / task_id
        if artifact_dir.exists():
            files = list(artifact_dir.iterdir())
            assert len(files) == 1, f"Expected 1 artifact file, found {len(files)}"
        # If no artifact_dir, that means no artifacts were written — which is also fine
        # since complete_task without artifact_ref doesn't write a file

    def test_idempotent_result_has_correct_task_id(self, tmp_path):
        paths = _make_paths(tmp_path)
        svc = _make_service(paths)
        op_key = f"op_{uuid4().hex[:8]}"
        task_id = _make_task(svc, op_key)
        raw = _make_output(task_id)

        receive_execution_result(raw, task_id=task_id, session_key="sess_001", paths=paths)
        result = receive_execution_result(raw, task_id=task_id, session_key="sess_002", paths=paths)

        assert result.task_id == task_id

    def test_idempotency_audit_event_recorded(self, tmp_path):
        paths = _make_paths(tmp_path)
        svc = _make_service(paths)
        op_key = f"op_{uuid4().hex[:8]}"
        task_id = _make_task(svc, op_key)
        raw = _make_output(task_id)

        receive_execution_result(raw, task_id=task_id, session_key="sess_001", paths=paths)
        receive_execution_result(raw, task_id=task_id, session_key="sess_002", paths=paths)

        svc2 = _make_service(paths)
        events = svc2.db.list_audit_events(task_id)
        event_types = [e["event_type"] for e in events]
        # Second submission should produce an action_execution_rejected event
        assert "action_execution_rejected" in event_types


# ---------------------------------------------------------------------------
# service.execute_action: duplicate operation_key via service layer
# ---------------------------------------------------------------------------

class TestServiceExecuteActionIdempotency:
    def test_duplicate_execute_returns_existing_execution(self, tmp_path):
        paths = _make_paths(tmp_path)
        svc = _make_service(paths)
        op_key = f"op_{uuid4().hex[:8]}"
        task_id = _make_task(svc, op_key)

        first = svc.execute_action(task_id, "result one")
        assert first["duplicate"] is False

        second = svc.execute_action(task_id, "result two — should be ignored")
        assert second["duplicate"] is True
        assert second["duplicate_reason"] == "already_executed_for_task"
        # The existing execution record should be returned
        assert second["execution"] is not None
        assert second["execution"].operation_key == op_key

    def test_duplicate_execute_does_not_overwrite_result(self, tmp_path):
        paths = _make_paths(tmp_path)
        svc = _make_service(paths)
        op_key = f"op_{uuid4().hex[:8]}"
        task_id = _make_task(svc, op_key)

        svc.execute_action(task_id, "original result")
        svc.execute_action(task_id, "overwrite attempt — should be ignored")

        svc2 = _make_service(paths)
        execution = svc2.db.get_execution(op_key)
        assert execution.result_summary == "original result"
