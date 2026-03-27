"""
End-to-end smoke test — plan_first flow (Phase 7, Gap 5).

Tests the full lifecycle:
  create task → PM submits plan → CoS approves → execution callback → complete
  + duplicate callback idempotency
  + callback edge cases (unknown task, terminal task, session_key fallback)

Mocks: PaperclipClient is unused (AppConfig has no Paperclip config).
       Discord/Gmail notifiers are wrapped in try/except in service.py — fail silently.
No real network calls.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.agentic_os.config import AppConfig, Paths, repo_root
from src.agentic_os.models import RequestClassification
from src.agentic_os.service import AgenticOSService
from src.agentic_os.web import create_app


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


def _make_output(task_id: str, content: str = "Work done.") -> str:
    return (
        "RESULT_START\n"
        f"{content}\n"
        "RESULT_END\n"
        f"TASK_DONE: {task_id}\n"
    )


def _create_plan_first_task(svc: AgenticOSService) -> str:
    """Create a technical/execute/high-risk task which always enters plan_first mode."""
    classification = RequestClassification(
        domain="technical",
        intent_type="execute",
        risk_level="high",
    )
    result = svc.create_request(
        user_request="Refactor the authentication module",
        classification=classification,
    )
    return result["task"].id


# ---------------------------------------------------------------------------
# 1. Full plan_first lifecycle
# ---------------------------------------------------------------------------

class TestPlanFirstFullFlow:
    def _client_and_service(self, tmp_path):
        paths = _make_paths(tmp_path)
        svc = _make_service(paths)
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        return client, svc, paths

    def test_full_plan_first_lifecycle(self, tmp_path):
        client, svc, paths = self._client_and_service(tmp_path)

        with patch("src.agentic_os.api_routes.get_service", return_value=svc), \
             patch("src.agentic_os.api_routes.default_paths", return_value=paths):

            # ── Step 1: Create task ───────────────────────────────────────
            task_id = _create_plan_first_task(svc)
            task = svc.db.get_task(task_id)
            assert task.status == "planning"
            assert task.task_mode == "plan_first"

            # ── Step 2: PM submits plan ───────────────────────────────────
            resp = client.post(f"/api/tasks/{task_id}/submit-plan", json={
                "plan_text": "Step 1: Audit. Step 2: Refactor. Step 3: Test.",
                "paperclip_document_id": "doc-abc",
            })
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "ok"
            assert body["task_id"] == task_id
            assert body["plan_version"] == 1

            task = svc.db.get_task(task_id)
            assert task.status == "awaiting_plan_review"
            assert task.plan_version == 1

            # ── Step 3: CoS approves (reconciler sees APPROVE comment) ────
            # The reconciler calls service.approve_plan() when it sees an
            # APPROVE comment — we call it directly here.
            svc.approve_plan(task_id, revision_id="plan-v1-reconciler")

            task = svc.db.get_task(task_id)
            assert task.status == "approved_for_execution"
            assert task.approved_plan_revision_id == "plan-v1-reconciler"

            # ── Step 4: Execution callback ────────────────────────────────
            output = _make_output(task_id, "Auth module refactored successfully.")
            resp = client.post("/api/executions/callback", json={
                "task_id": task_id,
                "session_key": "sess-abc",
                "output": output,
            })
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "success"

            task = svc.db.get_task(task_id)
            assert task.status in {"completed", "executed"}

            # ── Step 5: Duplicate callback — idempotent ───────────────────
            resp2 = client.post("/api/executions/callback", json={
                "task_id": task_id,
                "session_key": "sess-abc",
                "output": output,
            })
            assert resp2.status_code == 200
            body2 = resp2.json()
            assert body2["status"] == "success"
            assert body2["idempotent"] is True

            task_after = svc.db.get_task(task_id)
            assert task_after.status == task.status  # unchanged

            # ── Step 6: Audit trail ───────────────────────────────────────
            events = svc.db.list_audit_events(task_id)
            event_types = [e["event_type"] for e in events]

            assert "task_created" in event_types
            assert "plan_submitted" in event_types
            assert "plan_approved" in event_types
            assert "execution_callback_received" in event_types
            assert "task_completed" in event_types

            # Ordering
            assert event_types.index("plan_submitted") < event_types.index("plan_approved")
            assert event_types.index("plan_approved") < event_types.index("execution_callback_received")

    def test_submit_plan_increments_version_on_retry(self, tmp_path):
        """Second plan submission (after revision request) bumps plan_version."""
        client, svc, paths = self._client_and_service(tmp_path)

        with patch("src.agentic_os.api_routes.get_service", return_value=svc), \
             patch("src.agentic_os.api_routes.default_paths", return_value=paths):

            task_id = _create_plan_first_task(svc)

            client.post(f"/api/tasks/{task_id}/submit-plan", json={"plan_text": "Plan v1"})
            task = svc.db.get_task(task_id)
            assert task.plan_version == 1

            # CoS requests revision — back to planning
            svc.reject_plan(task_id, feedback="Add risk section")
            task = svc.db.get_task(task_id)
            assert task.status == "planning"

            resp = client.post(f"/api/tasks/{task_id}/submit-plan", json={"plan_text": "Plan v2 with risks"})
            assert resp.status_code == 200
            assert resp.json()["plan_version"] == 2

            task = svc.db.get_task(task_id)
            assert task.plan_version == 2
            assert task.status == "awaiting_plan_review"


# ---------------------------------------------------------------------------
# 2. Callback hardening edge cases
# ---------------------------------------------------------------------------

class TestCallbackHardening:
    def _client(self, tmp_path):
        paths = _make_paths(tmp_path)
        svc = _make_service(paths)
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        return client, svc, paths

    def test_unknown_task_id_returns_400(self, tmp_path):
        """POST /api/executions/callback with an unknown task_id → 400."""
        client, svc, paths = self._client(tmp_path)

        with patch("src.agentic_os.api_routes.get_service", return_value=svc), \
             patch("src.agentic_os.api_routes.default_paths", return_value=paths):

            resp = client.post("/api/executions/callback", json={
                "task_id": "task_does_not_exist",
                "session_key": "sess-unknown",
                "output": _make_output("task_does_not_exist"),
            })
            assert resp.status_code == 400

    def test_session_key_fallback_resolves_task(self, tmp_path):
        """Callback with blank task_id but valid session_key succeeds via fallback lookup."""
        client, svc, paths = self._client(tmp_path)

        with patch("src.agentic_os.api_routes.get_service", return_value=svc), \
             patch("src.agentic_os.api_routes.default_paths", return_value=paths):

            # Create task and record its dispatch session key
            classification = RequestClassification(
                domain="technical", intent_type="read", risk_level="low"
            )
            result = svc.create_request(
                user_request="Read something",
                classification=classification,
            )
            task_id = result["task"].id
            session_key = "sess-fallback-xyz"
            svc.db.update_dispatch_session_key(task_id, session_key)

            # Callback with blank task_id but correct session_key
            output = _make_output(task_id)
            resp = client.post("/api/executions/callback", json={
                "task_id": "",
                "session_key": session_key,
                "output": output,
            })
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "success"

            task = svc.db.get_task(task_id)
            assert task.status in {"completed", "executed"}

    def test_already_terminal_failed_returns_already_terminal(self, tmp_path):
        """Callback on a failed task → already_terminal response."""
        client, svc, paths = self._client(tmp_path)

        with patch("src.agentic_os.api_routes.get_service", return_value=svc), \
             patch("src.agentic_os.api_routes.default_paths", return_value=paths):

            classification = RequestClassification(
                domain="technical", intent_type="read", risk_level="low"
            )
            result = svc.create_request(
                user_request="Read something",
                classification=classification,
            )
            task_id = result["task"].id
            svc.fail_task(task_id, reason="upstream error")

            task = svc.db.get_task(task_id)
            assert task.status == "failed"

            resp = client.post("/api/executions/callback", json={
                "task_id": task_id,
                "session_key": "sess-late",
                "output": _make_output(task_id),
            })
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "already_terminal"
            assert body["task_status"] == "failed"

    def test_already_terminal_cancelled_returns_already_terminal(self, tmp_path):
        """Callback on a cancelled task → already_terminal response."""
        client, svc, paths = self._client(tmp_path)

        with patch("src.agentic_os.api_routes.get_service", return_value=svc), \
             patch("src.agentic_os.api_routes.default_paths", return_value=paths):

            classification = RequestClassification(
                domain="technical", intent_type="read", risk_level="low"
            )
            result = svc.create_request(
                user_request="Read something",
                classification=classification,
            )
            task_id = result["task"].id
            svc.cancel_task(task_id, reason="operator cancelled")

            resp = client.post("/api/executions/callback", json={
                "task_id": task_id,
                "session_key": "sess-late",
                "output": _make_output(task_id),
            })
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "already_terminal"
            assert body["task_status"] == "cancelled"

    def test_duplicate_completed_callback_is_idempotent(self, tmp_path):
        """Duplicate callback on a completed task → idempotent=True, no 5xx."""
        client, svc, paths = self._client(tmp_path)

        with patch("src.agentic_os.api_routes.get_service", return_value=svc), \
             patch("src.agentic_os.api_routes.default_paths", return_value=paths):

            classification = RequestClassification(
                domain="technical", intent_type="read", risk_level="low"
            )
            result = svc.create_request(
                user_request="Read something",
                classification=classification,
            )
            task_id = result["task"].id
            output = _make_output(task_id)

            first = client.post("/api/executions/callback", json={
                "task_id": task_id, "session_key": "s1", "output": output
            })
            assert first.status_code == 200
            assert first.json()["status"] == "success"

            second = client.post("/api/executions/callback", json={
                "task_id": task_id, "session_key": "s2", "output": output
            })
            assert second.status_code == 200
            assert second.json()["idempotent"] is True
            assert second.json()["status"] == "success"

    def test_callback_never_returns_5xx(self, tmp_path):
        """Malformed output → 200 error response, never 500."""
        client, svc, paths = self._client(tmp_path)

        with patch("src.agentic_os.api_routes.get_service", return_value=svc), \
             patch("src.agentic_os.api_routes.default_paths", return_value=paths):

            # Completely garbled output — no RESULT_START/RESULT_END
            resp = client.post("/api/executions/callback", json={
                "task_id": "anything",
                "session_key": "s1",
                "output": "this is not valid agent output",
            })
            assert resp.status_code != 500


# ---------------------------------------------------------------------------
# 3. submit-plan endpoint unit tests
# ---------------------------------------------------------------------------

class TestSubmitPlanEndpoint:
    def test_submit_plan_on_nonexistent_task_returns_404(self, tmp_path):
        paths = _make_paths(tmp_path)
        svc = _make_service(paths)
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch("src.agentic_os.api_routes.get_service", return_value=svc):
            resp = client.post("/api/tasks/no-such-task/submit-plan", json={
                "plan_text": "A plan",
            })
            assert resp.status_code in {404, 400}

    def test_submit_plan_on_direct_task_returns_error(self, tmp_path):
        """submit-plan on a non-plan_first task should return an error."""
        paths = _make_paths(tmp_path)
        svc = _make_service(paths)
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch("src.agentic_os.api_routes.get_service", return_value=svc):
            classification = RequestClassification(
                domain="personal", intent_type="read", risk_level="low"
            )
            result = svc.create_request(
                user_request="Read emails",
                classification=classification,
            )
            task_id = result["task"].id
            task = svc.db.get_task(task_id)
            # Only plan_first tasks accept submit-plan
            if task.task_mode != "plan_first":
                resp = client.post(f"/api/tasks/{task_id}/submit-plan", json={
                    "plan_text": "Should fail",
                })
                assert resp.status_code in {400, 422, 500}
