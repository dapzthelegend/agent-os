from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from src.agentic_os.config import AppConfig, Paths
from src.agentic_os.execution_receiver import receive_execution_result
from src.agentic_os.models import RequestClassification
from src.agentic_os.service import AgenticOSService


def _make_service(tmp_path: Path) -> AgenticOSService:
    service = AgenticOSService(Paths.from_root(tmp_path), AppConfig())
    service.initialize()
    return service


def _create_contribution_task(service: AgenticOSService):
    payload = service.create_request(
        user_request="Contribute a fix to owner/repo",
        classification=RequestClassification(
            domain="technical",
            intent_type="execute",
            risk_level="medium",
        ),
        agent_key="engineer",
        operation_key=f"github-contribution-{uuid4().hex}",
        request_metadata={
            "task_kind": "github_contribution",
            "repo": "owner/repo",
            "repo_name": "repo",
            "repo_strategy_path": "/Users/dara/agents/projects/technical/repo-strategy/repo.md",
            "contribution_lifecycle_state": "no_pr_started",
        },
    )
    return payload["task"]


def _callback_output(task_id: str, payload: dict[str, object], body: str = "Updated contribution.") -> str:
    block = json.dumps({"github_contribution": payload}, indent=2, sort_keys=True)
    return (
        "RESULT_START\n"
        f"{body}\n\n"
        "```json\n"
        f"{block}\n"
        "```\n"
        "RESULT_END\n"
        f"TASK_DONE: {task_id}\n"
    )


def test_open_pr_result_keeps_contribution_task_active(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    task = _create_contribution_task(service)

    output = _callback_output(
        task.id,
        {
            "repo": "owner/repo",
            "repo_name": "repo",
            "repo_strategy_path": "/Users/dara/agents/projects/technical/repo-strategy/repo.md",
            "linked_pr_number": 42,
            "linked_pr_url": "https://github.com/owner/repo/pull/42",
            "lifecycle_state": "pr_open_under_review",
            "pr_status": "open",
            "responsible_assignee": "engineer",
            "strategy_memory_updated": False,
        },
    )

    result = receive_execution_result(
        output,
        task_id=task.id,
        session_key="session-open-pr",
        paths=service.paths,
    )
    assert result.success is True

    updated = service.db.get_task(task.id)
    assert updated.status == "in_progress"
    metadata = json.loads(updated.request_metadata_json or "{}")
    assert metadata["task_kind"] == "github_contribution"
    assert metadata["github_contribution"]["pr_status"] == "open"
    assert metadata["github_contribution"]["linked_pr_number"] == 42


def test_updating_existing_open_pr_still_does_not_complete_task(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    task = _create_contribution_task(service)

    first_output = _callback_output(
        task.id,
        {
            "repo": "owner/repo",
            "repo_name": "repo",
            "linked_pr_number": 42,
            "linked_pr_url": "https://github.com/owner/repo/pull/42",
            "lifecycle_state": "pr_open_under_review",
            "pr_status": "open",
            "responsible_assignee": "engineer",
            "strategy_memory_updated": False,
        },
    )
    receive_execution_result(first_output, task_id=task.id, session_key="session-1", paths=service.paths)

    second_output = _callback_output(
        task.id,
        {
            "repo": "owner/repo",
            "repo_name": "repo",
            "linked_pr_number": 42,
            "linked_pr_url": "https://github.com/owner/repo/pull/42",
            "lifecycle_state": "followup_required",
            "pr_status": "open",
            "responsible_assignee": "engineer",
            "next_followup_hint": "Address requested changes on the existing PR.",
            "strategy_memory_updated": False,
        },
        body="Updated the existing PR after review.",
    )
    receive_execution_result(second_output, task_id=task.id, session_key="session-2", paths=service.paths)

    updated = service.db.get_task(task.id)
    assert updated.status == "in_progress"
    metadata = json.loads(updated.request_metadata_json or "{}")
    assert metadata["github_contribution"]["lifecycle_state"] == "followup_required"
    assert metadata["github_contribution"]["pr_status"] == "open"


def test_merged_pr_is_the_only_completion_condition(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    task = _create_contribution_task(service)

    output = _callback_output(
        task.id,
        {
            "repo": "owner/repo",
            "repo_name": "repo",
            "linked_pr_number": 42,
            "linked_pr_url": "https://github.com/owner/repo/pull/42",
            "lifecycle_state": "merged_ready_to_close",
            "pr_status": "merged",
            "responsible_assignee": "engineer",
            "strategy_memory_updated": True,
        },
        body="The PR merged and the contribution is ready to close.",
    )
    receive_execution_result(output, task_id=task.id, session_key="session-merged", paths=service.paths)

    updated = service.db.get_task(task.id)
    assert updated.status == "done"
    metadata = json.loads(updated.request_metadata_json or "{}")
    assert metadata["github_contribution"]["pr_status"] == "merged"


def test_closed_unmerged_pr_stays_not_done(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    task = _create_contribution_task(service)

    output = _callback_output(
        task.id,
        {
            "repo": "owner/repo",
            "repo_name": "repo",
            "linked_pr_number": 42,
            "linked_pr_url": "https://github.com/owner/repo/pull/42",
            "lifecycle_state": "closed_unmerged",
            "pr_status": "closed_unmerged",
            "resolution": "escalated",
            "responsible_assignee": "engineer",
            "strategy_memory_updated": False,
        },
        body="The PR closed without merge.",
    )
    receive_execution_result(output, task_id=task.id, session_key="session-closed", paths=service.paths)

    updated = service.db.get_task(task.id)
    assert updated.status == "to_do"
    metadata = json.loads(updated.request_metadata_json or "{}")
    assert metadata["github_contribution"]["pr_status"] == "closed_unmerged"
    assert metadata["github_contribution"]["resolution"] == "escalated"


def test_new_contribution_task_creation_still_works_with_under_review_work(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    first = _create_contribution_task(service)

    output = _callback_output(
        first.id,
        {
            "repo": "owner/repo",
            "repo_name": "repo",
            "linked_pr_number": 42,
            "linked_pr_url": "https://github.com/owner/repo/pull/42",
            "lifecycle_state": "waiting_on_maintainer",
            "pr_status": "open",
            "responsible_assignee": "engineer",
            "strategy_memory_updated": False,
        },
        body="Waiting on maintainer review.",
    )
    receive_execution_result(output, task_id=first.id, session_key="session-waiting", paths=service.paths)

    second = _create_contribution_task(service)
    assert service.db.get_task(first.id).status == "in_progress"
    assert service.db.get_task(second.id).status == "to_do"
