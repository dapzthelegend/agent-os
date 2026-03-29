from __future__ import annotations

import json
import shutil
from pathlib import Path

from src.agentic_os.config import AppConfig, PaperclipConfig, Paths, repo_root
from src.agentic_os.models import RequestClassification, TaskRecord
from src.agentic_os.service import AgenticOSService
from src.agentic_os.task_control_plane import TaskControlPlane


def _make_paths(tmp_path: Path) -> Paths:
    paths = Paths.from_root(tmp_path)
    src = repo_root() / "policy_rules.json"
    if src.exists():
        shutil.copyfile(src, paths.policy_rules_path)
    return paths


def _make_service(tmp_path: Path) -> AgenticOSService:
    service = AgenticOSService(_make_paths(tmp_path), AppConfig())
    service.initialize()
    return service


def _make_task_with_metadata(metadata: dict) -> TaskRecord:
    return TaskRecord(
        id="task_1",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        domain="system",
        intent_type="execute",
        risk_level="medium",
        status="planning",
        approval_state="not_needed",
        user_request="runtime follow-up",
        result_summary=None,
        artifact_ref=None,
        external_ref=None,
        target=None,
        request_metadata_json=json.dumps(metadata),
        operation_key=None,
        external_write=False,
        policy_decision="approval_required",
        action_source="openclaw_skill",
    )


def _make_paperclip_config(include_infra: bool) -> PaperclipConfig:
    agent_map = {
        "chief_of_staff": "a",
        "project_manager": "b",
        "engineering_manager": "c",
        "engineer": "d",
        "executor_codex": "e",
        "content_writer": "f",
        "accountant": "g",
        "executive_assistant": "h",
    }
    if include_infra:
        agent_map["infrastructure_engineer"] = "i"
    return PaperclipConfig(
        base_url="http://localhost:3100/api",
        auth_mode="trusted",
        company_id="company",
        goal_id="goal",
        project_map={
            "personal": "p1",
            "technical": "p2",
            "finance": "p3",
            "system": "p4",
        },
        agent_map=agent_map,
    )


def test_incident_task_routes_to_infrastructure_engineer_when_available() -> None:
    cp = TaskControlPlane(_make_paperclip_config(include_infra=True))
    task = _make_task_with_metadata({"task_kind": "incident_remediation"})
    assert cp.resolve_executor_key(task) == "infrastructure_engineer"


def test_incident_task_falls_back_to_engineer_when_infra_agent_missing() -> None:
    cp = TaskControlPlane(_make_paperclip_config(include_infra=False))
    task = _make_task_with_metadata({"task_kind": "incident_remediation"})
    assert cp.resolve_executor_key(task) == "engineer"


def test_create_runtime_followup_task_deduplicates_by_operation_key(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    created = service.create_runtime_followup_task(
        summary="Follow up: add runtime heartbeat monitor",
        kind="actionable_followup",
        dedupe_key="heartbeat-monitor",
    )
    duplicate = service.create_runtime_followup_task(
        summary="Follow up: add runtime heartbeat monitor",
        kind="actionable_followup",
        dedupe_key="heartbeat-monitor",
    )

    assert created["deduplicated"] is False
    assert duplicate["deduplicated"] is True
    assert duplicate["task"].id == created["task"].id


def test_record_spawn_failure_creates_incident_followup(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    result = service.create_request(
        user_request="Run runtime worker for technical request",
        classification=RequestClassification(
            domain="technical",
            intent_type="read",
            risk_level="low",
        ).validate(),
        action_source="manual",
    )
    task = result["task"]

    service.record_spawn_failure(task.id, reason="gateway unavailable")

    followups = []
    for item in service.list_tasks(limit=20):
        if not item.request_metadata_json:
            continue
        metadata = json.loads(item.request_metadata_json)
        if metadata.get("task_kind") == "incident_remediation":
            followups.append((item, metadata))

    assert followups
    followup_task, metadata = followups[0]
    assert metadata.get("origin_task_id") == task.id
    assert followup_task.domain == "system"
    assert followup_task.intent_type == "execute"


def test_high_risk_followup_forces_explicit_approval(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    payload = service.create_runtime_followup_task(
        summary="Rotate global runtime credentials safely",
        kind="actionable_followup",
        risk_level="high",
        dedupe_key="rotate-creds",
    )
    task = payload["task"]
    metadata = json.loads(task.request_metadata_json or "{}")
    assert metadata.get("require_explicit_approval") is True
    assert task.task_mode == "direct"
    assert task.status == "awaiting_approval"
