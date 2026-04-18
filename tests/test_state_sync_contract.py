from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.agentic_os.config import AppConfig, PaperclipConfig, Paths, repo_root
from src.agentic_os.execution_receiver import receive_execution_result
from src.agentic_os.models import RequestClassification
from src.agentic_os.approval_capability import mint_approval_token
from src.agentic_os.paperclip_client import DocumentRef, IssueRef, PaperclipClient
from src.agentic_os.paperclip_reconciler import PaperclipReconciler
from src.agentic_os.service import AgenticOSService
from src.agentic_os.task_control_plane import ResultWritebackOutcome, TaskControlPlane


def _make_paths(tmp_path: Path) -> Paths:
    paths = Paths.from_root(tmp_path)
    src = repo_root() / "policy_rules.json"
    if src.exists():
        shutil.copyfile(src, paths.policy_rules_path)
    return paths


def _make_service(tmp_path: Path) -> AgenticOSService:
    svc = AgenticOSService(_make_paths(tmp_path), AppConfig())
    svc.initialize()
    return svc


def _create_task(service: AgenticOSService, *, status: str, issue_id: str, operation_key: str | None = None):
    task = service.db.create_task(
        classification=RequestClassification(
            domain="technical",
            intent_type="execute",
            risk_level="medium",
            status=status,
            approval_state="not_needed",
        ),
        user_request="sync test",
        policy_decision="read_ok",
        operation_key=operation_key,
    )
    return service.db.update_task(task.id, paperclip_issue_id=issue_id)


def test_triage_paperclip_status_at_import() -> None:
    assert AgenticOSService._triage_paperclip_status("backlog") == "to_do"
    assert AgenticOSService._triage_paperclip_status("todo") == "to_do"
    assert AgenticOSService._triage_paperclip_status("in_review") == "to_do"
    assert AgenticOSService._triage_paperclip_status("blocked") == "to_do"
    assert AgenticOSService._triage_paperclip_status("in_progress") == "in_progress"
    assert AgenticOSService._triage_paperclip_status("done") == "done"
    assert AgenticOSService._triage_paperclip_status("cancelled") == "done"


def test_reconciler_does_not_mirror_status_changes(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    task = _create_task(service, status="to_do", issue_id="iss-1")

    class FakeCP:
        def list_all_issues(self, *, limit: int = 100):
            return [IssueRef(id="iss-1", title="T", status="in_progress")]

        def promote_issue_to_todo(self, issue_id: str, task):
            raise AssertionError("unexpected promote call")

    reconciler = PaperclipReconciler(service.paths, AppConfig())
    reconciler._service = service
    reconciler._cp = FakeCP()

    result = reconciler.run_once()
    updated = service.db.get_task(task.id)
    assert "mirrored" not in result
    assert updated.status == "to_do"


def test_no_lifecycle_status_writebacks_to_paperclip(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    task = _create_task(service, status="to_do", issue_id="iss-2")

    class FakeClient:
        def update_issue(self, *args, **kwargs):  # pragma: no cover - must never be called
            raise AssertionError("status writeback attempted")

    class FakeCP:
        def __init__(self):
            self._client = FakeClient()

        def write_result(self, issue_id: str, result_text: str, *, task_id: str, artifact_path=None) -> None:
            raise AssertionError("result writeback attempted")

        def post_failure_comment(self, issue_id: str, reason: str):
            return None

        def add_comment(self, issue_id: str, body: str):
            return None

    service._cp_cache = FakeCP()
    service._cp_initialized = True

    pickup = service.pickup_task(task.id)
    assert pickup["success"] is True
    service.complete_task(task.id, "ok")

    task2 = _create_task(service, status="to_do", issue_id="iss-3")
    service.fail_task(task2.id, "fail")


def test_execution_callback_writes_result_back_as_document_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(tmp_path)
    task = _create_task(service, status="to_do", issue_id="iss-doc")
    service.pickup_task(task.id)

    calls: list[dict[str, str]] = []

    class FakeCP:
        def write_result(self, issue_id: str, result_text: str, *, task_id: str, artifact_path=None) -> ResultWritebackOutcome:
            calls.append(
                {
                    "issue_id": issue_id,
                    "task_id": task_id,
                    "result_text": result_text,
                    "artifact_path": str(artifact_path),
                }
            )
            return ResultWritebackOutcome(issue_id=issue_id, task_id=task_id, wrote_document=True, uploaded_artifact=True)

    monkeypatch.setattr(AgenticOSService, "_cp", property(lambda self: FakeCP()))

    result = receive_execution_result(
        f"RESULT_START\n# Result\nRESULT_END\nTASK_DONE: {task.id}",
        task_id=task.id,
        session_key="session-1",
        paths=service.paths,
    )

    assert result.success is True
    assert len(calls) == 1
    assert calls[0]["issue_id"] == "iss-doc"
    assert calls[0]["task_id"] == task.id
    assert calls[0]["artifact_path"].endswith(".md")


def test_execution_callback_flags_orphaned_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _make_service(tmp_path)
    task = _create_task(service, status="to_do", issue_id="iss-orphan")
    service.pickup_task(task.id)
    dead_letter_dir = str(service.paths.data_dir / "orphaned_results" / "sample")

    class FakeCP:
        def write_result(self, issue_id: str, result_text: str, *, task_id: str, artifact_path=None) -> ResultWritebackOutcome:
            return ResultWritebackOutcome(
                issue_id=issue_id,
                task_id=task_id,
                orphaned=True,
                dead_letter_dir=dead_letter_dir,
                errors=["HTTP Error 404: Not Found"],
            )

    monkeypatch.setattr(AgenticOSService, "_cp", property(lambda self: FakeCP()))

    result = receive_execution_result(
        f"RESULT_START\n# Result\nRESULT_END\nTASK_DONE: {task.id}",
        task_id=task.id,
        session_key="session-2",
        paths=service.paths,
    )

    assert result.success is True
    updated = service.db.get_task(task.id)
    metadata = json.loads(updated.request_metadata_json or "{}")
    assert metadata["result_orphaned"] is True
    assert metadata["result_orphaned_issue_id"] == "iss-orphan"
    assert metadata["result_orphaned_dead_letter_dir"] == dead_letter_dir
    assert any(event["event_type"] == "paperclip_result_orphaned" for event in service.db.list_audit_events(task.id))


def test_control_plane_has_update_issue_status_for_approval_promotion() -> None:
    assert hasattr(TaskControlPlane, "update_issue_status")


def test_paperclip_issue_creation_defaults_to_backlog() -> None:
    captured: dict[str, object] = {}
    client = PaperclipClient(
        PaperclipConfig(
            base_url="http://127.0.0.1:3100/api",
            auth_mode="trusted",
            company_id="company-1",
            goal_id="goal-1",
            project_map={"personal": "p1", "technical": "p2", "finance": "p3", "system": "p4"},
            agent_map={
                "chief_of_staff": "a1",
                "project_manager": "a2",
                "engineering_manager": "a3",
                "engineer": "a4",
                "infrastructure_engineer": "a5",
                "executor_codex": "a6",
                "content_writer": "a7",
                "accountant": "a8",
                "executive_assistant": "a9",
            },
        )
    )

    def fake_request(method: str, path: str, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {
            "id": "iss-100",
            "title": body.get("title", ""),
            "status": body.get("status", ""),
            "description": body.get("description", ""),
        }

    client._request = fake_request  # type: ignore[attr-defined]
    issue = client.create_issue(
        title="Task",
        description="Desc",
        project_id="p2",
        goal_id="goal-1",
        assignee_id="a4",
    )
    assert issue.status == "backlog"
    assert isinstance(captured.get("body"), dict)
    assert captured["body"]["status"] == "backlog"


def test_paperclip_client_wake_agent_posts_assignment_wake() -> None:
    captured: dict[str, object] = {}
    client = PaperclipClient(
        PaperclipConfig(
            base_url="http://127.0.0.1:3100/api",
            auth_mode="trusted",
            company_id="company-1",
            goal_id="goal-1",
            project_map={"personal": "p1", "technical": "p2", "finance": "p3", "system": "p4"},
            agent_map={
                "chief_of_staff": "a1",
                "project_manager": "a2",
                "engineering_manager": "a3",
                "engineer": "a4",
                "infrastructure_engineer": "a5",
                "executor_codex": "a6",
                "content_writer": "a7",
                "accountant": "a8",
                "executive_assistant": "a9",
            },
        )
    )

    def fake_request(method: str, path: str, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"status": "queued"}

    client._request = fake_request  # type: ignore[attr-defined]
    _ = client.wake_agent(
        "agent-1",
        source="assignment",
        trigger_detail="system",
        reason="issue_assigned",
        payload={"issueId": "iss-1"},
    )
    assert captured["method"] == "POST"
    assert captured["path"] == "/agents/agent-1/wakeup"
    assert isinstance(captured.get("body"), dict)
    assert captured["body"]["source"] == "assignment"
    assert captured["body"]["reason"] == "issue_assigned"


def test_control_plane_update_issue_status_relies_on_paperclip_status_change_wake() -> None:
    cp = TaskControlPlane(
        PaperclipConfig(
            base_url="http://127.0.0.1:3100/api",
            auth_mode="trusted",
            company_id="company-1",
            goal_id="goal-1",
            project_map={"personal": "p1", "technical": "p2", "finance": "p3", "system": "p4"},
            agent_map={
                "chief_of_staff": "a1",
                "project_manager": "a2",
                "engineering_manager": "a3",
                "engineer": "a4",
                "infrastructure_engineer": "a5",
                "executor_codex": "a6",
                "content_writer": "a7",
                "accountant": "a8",
                "executive_assistant": "a9",
            },
        )
    )

    class FakeClient:
        def __init__(self):
            self.get_calls = 0

        def get_issue(self, issue_id: str):
            self.get_calls += 1
            return IssueRef(id=issue_id, title="T", status="backlog", assignee_id="a4")

        def update_issue(self, issue_id: str, **kwargs):
            return IssueRef(id=issue_id, title="T", status=kwargs.get("status", "todo"), assignee_id="a4")

    fake_client = FakeClient()
    cp._client = fake_client  # type: ignore[assignment]
    updated = cp.update_issue_status("iss-1", "approved_for_execution")
    assert updated is not None
    assert fake_client.get_calls == 0


def test_control_plane_create_issue_enforces_brief_written() -> None:
    cp = TaskControlPlane(
        PaperclipConfig(
            base_url="http://127.0.0.1:3100/api",
            auth_mode="trusted",
            company_id="company-1",
            goal_id="goal-1",
            project_map={"personal": "p1", "technical": "p2", "finance": "p3", "system": "p4"},
            agent_map={
                "chief_of_staff": "a1",
                "project_manager": "a2",
                "engineering_manager": "a3",
                "engineer": "a4",
                "infrastructure_engineer": "a5",
                "executor_codex": "a6",
                "content_writer": "a7",
                "accountant": "a8",
                "executive_assistant": "a9",
            },
        )
    )

    class FakeClient:
        def __init__(self) -> None:
            self.brief_written = False
            self.write_calls = 0

        def create_issue(self, **kwargs):
            return IssueRef(
                id="iss-brief-1",
                title=str(kwargs.get("title", "Task")),
                status=str(kwargs.get("status", "todo")),
            )

        def list_documents(self, issue_id: str):
            if self.brief_written:
                return [
                    DocumentRef(
                        id="doc-1",
                        issue_id=issue_id,
                        title="agentic-os brief",
                        content="ok",
                        doc_type="brief",
                    )
                ]
            return []

        def write_document(self, issue_id: str, *, title: str, content: str, doc_type: str = "plan"):
            self.brief_written = True
            self.write_calls += 1
            return DocumentRef(id="doc-1", issue_id=issue_id, title=title, content=content, doc_type=doc_type)

    class FakeTask:
        id = "task_123"
        title = "Task"
        description = "Desc"
        user_request = "Task"
        domain = "technical"
        approval_state = "not_needed"
        status = "to_do"
        task_mode = "direct"

    fake_client = FakeClient()
    cp._client = fake_client  # type: ignore[assignment]
    issue = cp.create_issue(FakeTask(), assignee_key="engineer")
    assert issue is not None
    assert fake_client.write_calls == 1


def test_mark_dispatched_continues_when_brief_missing(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    task = _create_task(service, status="to_do", issue_id="iss-no-brief")

    class FakeCP:
        def ensure_task_brief(self, issue_id: str, task):  # noqa: ANN001
            return False

    service._cp_cache = FakeCP()
    service._cp_initialized = True

    service.mark_dispatched(task.id, session_key="sess-1", agent="engineer")
    updated = service.db.get_task(task.id)
    assert updated.dispatch_session_key == "sess-1"


def test_mark_dispatched_succeeds_when_brief_present(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    task = _create_task(service, status="to_do", issue_id="iss-brief-ok")

    class FakeCP:
        def ensure_task_brief(self, issue_id: str, task):  # noqa: ANN001
            return True

    service._cp_cache = FakeCP()
    service._cp_initialized = True

    service.mark_dispatched(task.id, session_key="sess-2", agent="engineer")
    updated = service.db.get_task(task.id)
    assert updated.dispatch_session_key == "sess-2"


def test_reconciler_promotes_backlog_to_todo_when_execution_ready(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    task = _create_task(service, status="to_do", issue_id="iss-ready")

    class FakeCP:
        promoted_ids: list[str]

        def __init__(self) -> None:
            self.promoted_ids = []

        def list_all_issues(self, *, limit: int = 100):
            return [IssueRef(id="iss-ready", title="Ready", status="backlog", project_id="proj-tech")]

        def promote_issue_to_todo(self, issue_id: str, task):
            self.promoted_ids.append(issue_id)
            return IssueRef(
                id=issue_id,
                title="Ready",
                status="todo",
                project_id="proj-tech",
                assignee_id="agent-eng",
            )

    fake_cp = FakeCP()
    reconciler = PaperclipReconciler(service.paths, AppConfig())
    reconciler._service = service
    reconciler._cp = fake_cp

    result = reconciler.run_once()
    assert result["promoted"] == 1
    assert fake_cp.promoted_ids == ["iss-ready"]
    updated = service.db.get_task(task.id)
    assert updated.paperclip_status == "todo"
    assert updated.paperclip_project_id == "proj-tech"
    assert updated.paperclip_assignee_agent_id == "agent-eng"


def test_reconciler_does_not_promote_when_approval_is_pending(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    task = _create_task(service, status="to_do", issue_id="iss-pending")
    service.db.update_task(task.id, policy_decision="approval_required", approval_state="pending")

    class FakeCP:
        def list_all_issues(self, *, limit: int = 100):
            return [IssueRef(id="iss-pending", title="Pending", status="backlog", project_id="proj-tech")]

        def promote_issue_to_todo(self, issue_id: str, task):  # pragma: no cover - must not be called
            raise AssertionError("promotion attempted while approval is pending")

    reconciler = PaperclipReconciler(service.paths, AppConfig())
    reconciler._service = service
    reconciler._cp = FakeCP()

    result = reconciler.run_once()
    assert result["promoted"] == 0


def test_deny_syncs_paperclip_issue_to_cancelled_without_comment_wake(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    payload = service.create_request(
        user_request="approval gated request",
        agent_key="engineer",
        classification=RequestClassification(
            domain="technical",
            intent_type="execute",
            risk_level="medium",
            status="to_do",
            approval_state="pending",
        ),
        operation_key="op-deny-sync-1",
    )
    task = payload["task"]
    approval = payload["approval"]
    assert approval is not None
    task = service.db.update_task(task.id, paperclip_issue_id="iss-deny-1")

    class FakeCP:
        def __init__(self) -> None:
            self.close_calls: list[str] = []
            self.comment_calls: list[tuple[str, str]] = []

        def close_issue_cancelled(self, issue_id: str):
            self.close_calls.append(issue_id)
            return None

        def add_comment(self, issue_id: str, body: str):
            self.comment_calls.append((issue_id, body))
            return None

    fake_cp = FakeCP()
    service._cp_cache = fake_cp
    service._cp_initialized = True

    result = service.deny(
        approval.id,
        decision_note="Approval denied.",
        approval_token=mint_approval_token(action="deny", approval_id=approval.id),
    )
    assert result["task"].status == "done"
    assert fake_cp.close_calls == ["iss-deny-1"]
    assert fake_cp.comment_calls == []


def test_cancel_syncs_paperclip_issue_to_cancelled_without_comment_wake(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    payload = service.create_request(
        user_request="approval gated request",
        agent_key="engineer",
        classification=RequestClassification(
            domain="technical",
            intent_type="execute",
            risk_level="medium",
            status="to_do",
            approval_state="pending",
        ),
        operation_key="op-cancel-sync-1",
    )
    task = payload["task"]
    approval = payload["approval"]
    assert approval is not None
    task = service.db.update_task(task.id, paperclip_issue_id="iss-cancel-1")

    class FakeCP:
        def __init__(self) -> None:
            self.close_calls: list[str] = []
            self.comment_calls: list[tuple[str, str]] = []

        def close_issue_cancelled(self, issue_id: str):
            self.close_calls.append(issue_id)
            return None

        def add_comment(self, issue_id: str, body: str):
            self.comment_calls.append((issue_id, body))
            return None

    fake_cp = FakeCP()
    service._cp_cache = fake_cp
    service._cp_initialized = True

    result = service.cancel(
        approval.id,
        decision_note="Approval cancelled.",
        approval_token=mint_approval_token(action="cancel", approval_id=approval.id),
    )
    assert result["task"].status == "done"
    assert fake_cp.close_calls == ["iss-cancel-1"]
    assert fake_cp.comment_calls == []


def test_service_rejects_approval_mutation_without_capability_token(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    payload = service.create_request(
        user_request="approval gated request",
        agent_key="engineer",
        classification=RequestClassification(
            domain="technical",
            intent_type="execute",
            risk_level="medium",
            status="to_do",
            approval_state="pending",
        ),
        operation_key="op-capability-guard-1",
    )
    approval = payload["approval"]
    assert approval is not None

    with pytest.raises(Exception, match="dashboard and Discord surfaces"):
        service.approve(approval.id)


def test_paperclip_client_update_issue_can_clear_assignee() -> None:
    captured: dict[str, object] = {}
    client = PaperclipClient(
        PaperclipConfig(
            base_url="http://127.0.0.1:3100/api",
            auth_mode="trusted",
            company_id="company-1",
            goal_id="goal-1",
            project_map={"personal": "p1", "technical": "p2", "finance": "p3", "system": "p4"},
            agent_map={
                "chief_of_staff": "a1",
                "project_manager": "a2",
                "engineering_manager": "a3",
                "engineer": "a4",
                "infrastructure_engineer": "a5",
                "executor_codex": "a6",
                "content_writer": "a7",
                "accountant": "a8",
                "executive_assistant": "a9",
            },
        )
    )

    def fake_request(method: str, path: str, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"id": "iss-1", "title": "T", "status": "cancelled", "assigneeAgentId": None}

    client._request = fake_request  # type: ignore[attr-defined]
    _ = client.update_issue("iss-1", status="cancelled", clear_assignee=True)
    assert captured["method"] == "PATCH"
    assert captured["path"] == "/issues/iss-1"
    assert isinstance(captured.get("body"), dict)
    assert captured["body"]["status"] == "cancelled"
    assert "assigneeAgentId" in captured["body"]
    assert captured["body"]["assigneeAgentId"] is None


def test_resolve_response_includes_callback_instructions(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    task = _create_task(service, status="to_do", issue_id="iss-cb")

    result = service.ensure_task_for_paperclip_issue("iss-cb")
    assert result["found"] is True
    assert "callback" in result

    cb = result["callback"]
    assert cb["task_id"] == task.id
    assert cb["domain"] == task.domain
    assert cb["mode"] == task.task_mode
    assert task.id in cb["submit_result_cmd"]
    assert task.id in cb["submit_plan_cmd"]
    assert task.id in cb["result_file"]
    assert task.id in cb["plan_file"]


def test_runtime_never_executes_done_tasks_and_receiver_is_idempotent(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    task = _create_task(service, status="done", issue_id="iss-4", operation_key="op-1")

    ready_ids = [t.id for t in service.list_ready_tasks(limit=20)]
    assert task.id not in ready_ids

    pickup = service.pickup_task(task.id)
    assert pickup["success"] is False

    raw_output = f"""
RESULT_START
hello
RESULT_END
TASK_DONE: {task.id}
"""
    result = receive_execution_result(
        raw_output,
        task_id=task.id,
        session_key="session-1",
        paths=service.paths,
    )
    assert result.success is True
    assert result.idempotent is True


def test_end_to_end_import_triages_and_snapshots_paperclip_status(tmp_path: Path) -> None:
    service = _make_service(tmp_path)

    class FakeCP:
        def list_all_issues(self, *, limit: int = 100):
            return [
                IssueRef(id="iss-a", title="A", status="backlog", project_id="proj-tech"),
                IssueRef(id="iss-b", title="B", status="in_progress", project_id="proj-tech"),
                IssueRef(id="iss-c", title="C", status="done", project_id="proj-tech"),
            ]

        def promote_issue_to_todo(self, issue_id: str, task):
            if issue_id == "iss-a":
                return IssueRef(
                    id="iss-a",
                    title="A",
                    status="todo",
                    project_id="proj-tech",
                    assignee_id="agent-eng",
                )
            return None

    reconciler = PaperclipReconciler(service.paths, AppConfig())
    reconciler._service = service
    reconciler._cp = FakeCP()

    result = reconciler.run_once()
    assert result["imported"] == 3
    assert result["promoted"] == 0

    a = service.db.get_task_by_paperclip_issue_id("iss-a")
    b = service.db.get_task_by_paperclip_issue_id("iss-b")
    c = service.db.get_task_by_paperclip_issue_id("iss-c")
    # Backend pipeline status is determined by policy, not Paperclip state.
    # System-domain tasks require approval, so all enter as to_do/pending.
    # paperclip_status captures the actual Paperclip state as a snapshot.
    assert a is not None and a.status == "to_do"
    assert a.paperclip_status == "backlog"
    assert a.approval_state == "pending"
    assert b is not None and b.status == "to_do"
    assert b.paperclip_status == "in_progress"
    assert b.approval_state == "pending"
    assert c is not None and c.status == "to_do"
    assert c.paperclip_status == "done"
    assert c.approval_state == "pending"


def test_reconciler_imports_routine_issue_with_origin_metadata(tmp_path: Path) -> None:
    service = _make_service(tmp_path)

    class FakeCP:
        def list_all_issues(self, *, limit: int = 100):
            return [
                IssueRef(
                    id="iss-r1",
                    title="Routine task",
                    status="todo",
                    project_id="proj-tech",
                    routine_id="routine-1",
                    routine_run_id="run-1",
                    origin_kind="routine_execution",
                )
            ]

        def promote_issue_to_todo(self, issue_id: str, task):  # pragma: no cover - not called in this scenario
            raise AssertionError("unexpected promote call")

    reconciler = PaperclipReconciler(service.paths, AppConfig())
    reconciler._service = service
    reconciler._cp = FakeCP()

    result = reconciler.run_once()
    assert result["imported"] == 1
    task = service.db.get_task_by_paperclip_issue_id("iss-r1")
    assert task is not None
    assert task.action_source == "paperclip_routine"
    assert task.paperclip_origin_kind == "routine_execution"
    assert task.paperclip_routine_id == "routine-1"
    assert task.paperclip_routine_run_id == "run-1"


@pytest.mark.skip(reason="Temporarily disabled: flaky local import path for ollama policy module")
def test_policy_fallback_keeps_paperclip_routine_read_ok_when_llm_unavailable(tmp_path: Path, monkeypatch) -> None:
    service = _make_service(tmp_path)
    classification = RequestClassification(
        domain="technical",
        intent_type="execute",
        risk_level="medium",
    )

    monkeypatch.setattr(
        "src.agentic_os.ollama_policy.evaluate_policy_llm",
        lambda **_: None,
    )

    decision = service.evaluate_policy(
        classification=classification,
        target=None,
        external_write=False,
        action_source="paperclip_routine",
    )
    assert decision == "read_ok"
