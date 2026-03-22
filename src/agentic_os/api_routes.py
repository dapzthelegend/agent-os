from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .models import OperatorError
from .web_support import (
    annotate_audit_events,
    approval_groups,
    build_overview,
    enrich_approval_detail,
    enrich_task_detail,
    format_json,
    get_service,
    parse_artifact_input,
    serialize_task,
)


router = APIRouter(prefix="/api", tags=["api"])


class ApprovalDecisionPayload(BaseModel):
    note: Optional[str] = None


class ArtifactRevisionPayload(BaseModel):
    artifact_type: Optional[str] = None
    artifact_text: str = ""
    artifact_json: str = ""


@router.get("/overview")
def api_overview() -> dict:
    return build_overview(get_service())


@router.get("/tasks")
def api_tasks(
    limit: int = 50,
    status: Optional[str] = None,
    domain: Optional[str] = None,
    target: Optional[str] = None,
    action_source: Optional[str] = None,
) -> dict:
    service = get_service()
    tasks = service.list_tasks(
        limit=limit,
        status=status,
        domain=domain,
        target=target,
        action_source=action_source,
    )
    return {
        "filters": {
            "limit": limit,
            "status": status,
            "domain": domain,
            "target": target,
            "action_source": action_source,
        },
        "tasks": [serialize_task(task) for task in tasks],
    }


@router.get("/tasks/{task_id}")
def api_task_detail(task_id: str) -> dict:
    try:
        detail = enrich_task_detail(get_service(), task_id, artifact_id=None)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    detail["audit_events"] = annotate_audit_events(detail["audit_events"])
    if detail["selected_artifact"] is not None:
        detail["selected_artifact_pretty"] = detail["selected_artifact_content"]
    return detail


@router.get("/approvals")
def api_approvals(task_id: Optional[str] = None) -> dict:
    service = get_service()
    approvals = service.list_approvals(task_id=task_id)
    groups = approval_groups(service) if task_id is None else {}
    return {"approvals": [asdict(approval) for approval in approvals], "groups": groups}


@router.get("/approvals/{approval_id}")
def api_approval_detail(approval_id: str) -> dict:
    try:
        detail = enrich_approval_detail(get_service(), approval_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    detail["approval_payload_pretty"] = format_json(detail["approval_payload"])
    return detail


@router.get("/executions/{operation_key}")
def api_execution_detail(operation_key: str) -> dict:
    try:
        detail = get_service().get_execution_detail(operation_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if detail.get("audit_events"):
        detail["audit_events"] = annotate_audit_events(detail["audit_events"])
    return detail


@router.get("/audit")
def api_audit(limit: int = 50, domain: Optional[str] = None, target: Optional[str] = None) -> dict:
    payload = get_service().list_recent_audit_activity(limit=limit, domain=domain, target=target)
    payload["events"] = annotate_audit_events(payload["events"])
    return payload


@router.get("/recap/today")
def api_recap_today(domain: Optional[str] = None) -> dict:
    return get_service().recap_today(domain=domain)


@router.get("/recap/approvals")
def api_recap_approvals(domain: Optional[str] = None) -> dict:
    return get_service().recap_approvals(domain=domain)


@router.get("/recap/drafts")
def api_recap_drafts(domain: Optional[str] = None) -> dict:
    return get_service().recap_drafts(domain=domain)


@router.get("/recap/failures")
def api_recap_failures(domain: Optional[str] = None, limit: int = 20) -> dict:
    return get_service().recap_failures(domain=domain, limit=limit)


@router.get("/recap/external-actions")
def api_recap_external_actions(domain: Optional[str] = None, limit: int = 20) -> dict:
    return get_service().recap_external_actions(domain=domain, limit=limit)


def _handle_operator_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, OperatorError):
        return HTTPException(status_code=400, detail={"message": str(exc), "details": exc.details})
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.post("/approvals/{approval_id}/approve")
def api_approve(approval_id: str, payload: ApprovalDecisionPayload) -> dict:
    try:
        return get_service().approve(approval_id, decision_note=payload.note)
    except Exception as exc:
        raise _handle_operator_error(exc) from exc


@router.post("/approvals/{approval_id}/deny")
def api_deny(approval_id: str, payload: ApprovalDecisionPayload) -> dict:
    try:
        return get_service().deny(approval_id, decision_note=payload.note)
    except Exception as exc:
        raise _handle_operator_error(exc) from exc


@router.post("/approvals/{approval_id}/cancel")
def api_cancel(approval_id: str, payload: ApprovalDecisionPayload) -> dict:
    try:
        return get_service().cancel(approval_id, decision_note=payload.note)
    except Exception as exc:
        raise _handle_operator_error(exc) from exc


@router.post("/artifacts/{task_id}/revise")
def api_revise_artifact(task_id: str, payload: ArtifactRevisionPayload) -> dict:
    try:
        artifact_content = parse_artifact_input(payload.artifact_text, payload.artifact_json)
        return get_service().revise_artifact(
            task_id,
            artifact_type=payload.artifact_type,
            artifact_content=artifact_content,
        )
    except Exception as exc:
        raise _handle_operator_error(exc) from exc
