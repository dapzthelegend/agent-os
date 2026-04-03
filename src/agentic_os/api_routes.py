from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .config import default_paths, load_app_config
from .health import get_system_health
from .execution_receiver import ExecutionParseError, receive_execution_result
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


class RetryTaskPayload(BaseModel):
    feedback: str = "operator retry"


class ApprovalDecisionPayload(BaseModel):
    note: Optional[str] = None


class ArtifactRevisionPayload(BaseModel):
    artifact_type: Optional[str] = None
    artifact_text: str = ""
    artifact_json: str = ""


class ExecutionCallbackPayload(BaseModel):
    task_id: str
    session_key: str = "unknown"
    output: str


class ApprovePlanPayload(BaseModel):
    revision_id: str = ""  # defaults to "plan-v{N}-operator" if blank


class RejectPlanPayload(BaseModel):
    feedback: str


class CancelTaskPayload(BaseModel):
    reason: str = "Cancelled by operator"


class SetTaskModePayload(BaseModel):
    mode: str  # "plan_first" or "direct"


class SubmitPlanPayload(BaseModel):
    plan_text: str
    paperclip_document_id: Optional[str] = None


class CreateTaskPayload(BaseModel):
    user_request: str
    domain: str
    intent_type: str
    risk_level: str
    operation_key: Optional[str] = None
    target: Optional[str] = None
    request_metadata: Optional[dict] = None
    action_source: str = "api"


@router.get("/health")
def api_health() -> dict:
    return get_system_health(get_service())


@router.get("/overview")
def api_overview() -> dict:
    return build_overview(get_service())


@router.post("/tasks")
def api_create_task(payload: CreateTaskPayload) -> dict:
    """
    Create a new task via HTTP intake.

    Validates domain / intent_type / risk_level, runs the full create_request()
    pipeline (policy evaluation, plan gate, Paperclip projection), and returns
    the resulting task record.

    action_source defaults to "api". Use "manual" for operator-created tasks.
    operation_key is required when policy_decision resolves to "approval_required"
    and task_mode is "direct".
    """
    from .models import DOMAINS, INTENT_TYPES, RISK_LEVELS, RequestClassification

    errors = []
    if payload.domain not in DOMAINS:
        errors.append(f"domain must be one of: {', '.join(DOMAINS)}")
    if payload.intent_type not in INTENT_TYPES:
        errors.append(f"intent_type must be one of: {', '.join(INTENT_TYPES)}")
    if payload.risk_level not in RISK_LEVELS:
        errors.append(f"risk_level must be one of: {', '.join(RISK_LEVELS)}")
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    classification = RequestClassification(
        domain=payload.domain,
        intent_type=payload.intent_type,
        risk_level=payload.risk_level,
    )
    try:
        result = get_service().create_request(
            user_request=payload.user_request,
            classification=classification,
            operation_key=payload.operation_key,
            target=payload.target,
            request_metadata=payload.request_metadata,
            action_source=payload.action_source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise _handle_operator_error(exc) from exc

    task = result["task"]
    return {
        "task_id": task.id,
        "status": task.status,
        "task_mode": task.task_mode,
        "policy_decision": result["policy_decision"],
        "paperclip_issue_id": task.paperclip_issue_id,
    }


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


@router.get("/api/audit")
def api_audit_compat(limit: int = 50, domain: Optional[str] = None, target: Optional[str] = None) -> dict:
    """Compatibility alias for clients that already include '/api' in base URL."""
    return api_audit(limit=limit, domain=domain, target=target)


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


@router.post("/tasks/{task_id}/retry")
def api_retry_task(task_id: str, payload: RetryTaskPayload) -> dict:
    try:
        return get_service().retry_task(task_id, feedback=payload.feedback)
    except Exception as exc:
        raise _handle_operator_error(exc) from exc


@router.get("/recap/overdue")
def api_recap_overdue(domain: Optional[str] = None, threshold_hours: float = 48.0) -> dict:
    return get_service().recap_overdue(domain=domain, threshold_hours=threshold_hours)


@router.get("/recap/in-progress")
def api_recap_in_progress(domain: Optional[str] = None) -> dict:
    return get_service().recap_in_progress(domain=domain)


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


@router.post("/executions/callback")
def api_execution_callback(payload: ExecutionCallbackPayload) -> dict:
    """
    Receive ACP agent output and complete the execution pipeline.

    The agent output must contain RESULT_START...RESULT_END markers and
    TASK_DONE: <task_id>. Edge cases:
    - Unknown task_id (after session_key fallback): 400
    - Already terminal task: 200 { status: already_terminal }
    - Duplicate submission (operation_key dedup): 200 { status: success, idempotent: true }
    - Any internal error: 200 { status: error, reason: ... } — never 5xx
    """
    paths = default_paths()
    try:
        result = receive_execution_result(
            payload.output,
            task_id=payload.task_id,
            session_key=payload.session_key,
            paths=paths,
        )
        if result.task_not_found:
            try:
                get_service().create_runtime_incident_task(
                    summary="Execution callback referenced unknown task",
                    origin_task_id=payload.task_id or None,
                    origin_session_key=payload.session_key,
                    component="execution_callback_lookup",
                    error_type="task_not_found",
                    error_message=result.error or "task not found",
                )
            except Exception:
                pass
            raise HTTPException(
                status_code=400,
                detail={"status": "error", "reason": "task not found"},
            )
        if result.already_terminal:
            return {
                "status": "already_terminal",
                "task_id": result.task_id,
                "task_status": result.terminal_status,
            }
        return {
            "status": "success" if result.success else "error",
            "task_id": result.task_id,
            "artifact_id": result.artifact_id,
            "idempotent": result.idempotent,
            "error": result.error,
        }
    except HTTPException:
        raise
    except ExecutionParseError as exc:
        try:
            get_service().create_runtime_incident_task(
                summary="Execution callback payload could not be parsed",
                origin_task_id=payload.task_id,
                origin_session_key=payload.session_key,
                component="execution_callback_parser",
                error_type="execution_parse_error",
                error_message=str(exc),
            )
        except Exception:
            pass
        return {"status": "error", "reason": str(exc)}
    except Exception as exc:
        try:
            get_service().create_runtime_incident_task(
                summary="Execution callback processing crashed",
                origin_task_id=payload.task_id,
                origin_session_key=payload.session_key,
                component="execution_callback_handler",
                error_type="execution_callback_exception",
                error_message=str(exc),
            )
        except Exception:
            pass
        return {"status": "error", "reason": str(exc)}


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


@router.post("/tasks/{task_id}/approve-plan")
def api_approve_plan(task_id: str, payload: ApprovePlanPayload) -> dict:
    try:
        service = get_service()
        if not payload.revision_id:
            task = service.db.get_task(task_id)
            revision_id = f"plan-v{task.plan_version or 1}-operator"
        else:
            revision_id = payload.revision_id
        return asdict(service.approve_plan(task_id, revision_id=revision_id))
    except Exception as exc:
        raise _handle_operator_error(exc) from exc


@router.post("/tasks/{task_id}/reject-plan")
def api_reject_plan(task_id: str, payload: RejectPlanPayload) -> dict:
    try:
        return asdict(get_service().reject_plan(task_id, feedback=payload.feedback))
    except Exception as exc:
        raise _handle_operator_error(exc) from exc


@router.post("/tasks/{task_id}/cancel")
def api_cancel_task(task_id: str, payload: CancelTaskPayload) -> dict:
    try:
        return asdict(get_service().cancel_task(task_id, reason=payload.reason))
    except Exception as exc:
        raise _handle_operator_error(exc) from exc


@router.post("/tasks/{task_id}/submit-plan")
def api_submit_plan(task_id: str, payload: SubmitPlanPayload) -> dict:
    try:
        service = get_service()
        task = service.db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        version = (task.plan_version or 0) + 1
        service.submit_plan(task_id, payload.plan_text, version=version)
        return {"status": "ok", "task_id": task_id, "plan_version": version}
    except HTTPException:
        raise
    except Exception as exc:
        raise _handle_operator_error(exc) from exc


@router.post("/tasks/{task_id}/set-mode")
def api_set_task_mode(task_id: str, payload: SetTaskModePayload) -> dict:
    try:
        return asdict(get_service().set_task_mode(task_id, mode=payload.mode))
    except Exception as exc:
        raise _handle_operator_error(exc) from exc


@router.get("/paperclip/health")
def api_paperclip_health() -> dict:
    from .health import get_paperclip_health
    return get_paperclip_health(get_service())


@router.get("/paperclip/diagnostics")
def api_paperclip_diagnostics(
    task_id: Optional[str] = None,
    paperclip_issue_id: Optional[str] = None,
    activity_lookback_seconds: int = 86400,
) -> dict:
    from .health import get_paperclip_diagnostics

    try:
        return get_paperclip_diagnostics(
            get_service(),
            task_id=task_id,
            issue_id=paperclip_issue_id,
            activity_lookback_seconds=activity_lookback_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
