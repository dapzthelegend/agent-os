from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .config import default_paths, load_app_config
from .health import get_system_health, get_watchdog_status
from .execution_receiver import (
    ExecutionParseError,
    receive_execution_result,
    receive_execution_result_v2,
)
from .models import InvalidTransitionError, OperatorError
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


# ---------------------------------------------------------------------------
# Guards — clamp list limits and reject oversized payloads
# ---------------------------------------------------------------------------

_MAX_LIST_LIMIT = 200
_MAX_CALLBACK_OUTPUT_BYTES = 2 * 1024 * 1024  # 2 MB


def _clamp_limit(limit: int) -> int:
    """Clamp a caller-supplied list limit to a sane maximum."""
    return max(1, min(limit, _MAX_LIST_LIMIT))


router = APIRouter(prefix="/api", tags=["api"])


class RetryTaskPayload(BaseModel):
    feedback: str = "operator retry"


class ApprovalDecisionPayload(BaseModel):
    note: Optional[str] = None
    decided_by: Optional[str] = None


class ArtifactRevisionPayload(BaseModel):
    artifact_type: Optional[str] = None
    artifact_text: str = ""
    artifact_json: str = ""


class ExecutionCallbackPayload(BaseModel):
    """Dual-shape callback payload.

    v1 (legacy, still in use by bin/submit-result and existing agents):
      required: task_id, output (with RESULT_START/RESULT_END + TASK_DONE markers)
      optional: session_key

    v2 (new, emitted by agents that consume resolve-by-paperclip-issue's
    `writeback` block):
      required: task_id, execution_id, status
      required-when-succeeded: result (markdown)
      optional: session_key, metrics, output (not used)

    Shape is detected by presence of `execution_id`. Receiver logs
    `callback_shape_version` on every request for deprecation telemetry.
    """
    task_id: str
    session_key: str = "unknown"
    # v1
    output: Optional[str] = None
    # v2
    execution_id: Optional[str] = None
    status: Optional[str] = None  # succeeded | failed | blocked
    result: Optional[str] = None  # markdown, required when status=succeeded
    metrics: Optional[dict] = None


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


class BulkCloseTasksPayload(BaseModel):
    reason: str = "Closed by operator"


class CreateTaskPayload(BaseModel):
    user_request: str
    domain: str
    intent_type: str
    risk_level: str
    agent_key: str
    operation_key: Optional[str] = None
    target: Optional[str] = None
    request_metadata: Optional[dict] = None
    labels: Optional[list[str]] = None
    action_source: Optional[str] = None


@router.get("/health")
def api_health() -> dict:
    return get_system_health(get_service())


@router.get("/watchdog")
def api_watchdog() -> dict:
    return get_watchdog_status(get_service())


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
            agent_key=payload.agent_key,
            operation_key=payload.operation_key,
            target=payload.target,
            request_metadata=payload.request_metadata,
            labels=payload.labels,
            action_source=payload.action_source,
            default_action_source="api",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise _handle_operator_error(exc) from exc

    task = result["task"]
    return {
        "task_id": task.id,
        "status": task.status,
        "approval_state": task.approval_state,
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
    limit = _clamp_limit(limit)
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


@router.post("/tasks/close-all")
def api_bulk_close_tasks(payload: BulkCloseTasksPayload) -> dict:
    """
    Close all tasks that are in a closeable state, syncing each cancellation to Paperclip.

    Skips tasks that are already terminal (completed, executed, cancelled) or that
    cannot transition to cancelled (failed, stalled).
    """
    try:
        return get_service().bulk_close_tasks(reason=payload.reason)
    except Exception as exc:
        raise _handle_operator_error(exc) from exc


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


@router.get("/tasks/{task_id}/executions")
def api_task_executions(task_id: str) -> dict:
    """List all task_executions for a task, oldest ordinal first."""
    service = get_service()
    try:
        service.db.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    records = service.db.list_task_executions(task_id)
    return {
        "task_id": task_id,
        "executions": [asdict(r) for r in records],
    }


@router.get("/tasks/{task_id}/executions/{execution_id}")
def api_task_execution_detail(task_id: str, execution_id: str) -> dict:
    """Fetch a single task_execution. 404s if the execution's task_id doesn't
    match the path (prevents cross-task probing via execution_id)."""
    service = get_service()
    rec = service.db.get_task_execution(execution_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"unknown execution_id: {execution_id}")
    if rec.task_id != task_id:
        raise HTTPException(
            status_code=404,
            detail=f"execution {execution_id} does not belong to task {task_id}",
        )
    return {"execution": asdict(rec)}


class AuxiliaryArtifactPayload(BaseModel):
    label: str
    content_type: str
    content: str


@router.post("/executions/{execution_id}/artifacts")
def api_execution_artifact_upload(
    execution_id: str, payload: AuxiliaryArtifactPayload
) -> dict:
    """Attach an auxiliary (non-result) artifact — logs, diffs, JSON — to a
    task_execution. See service.create_auxiliary_artifact for allowlist and
    size cap.
    """
    service = get_service()
    try:
        return service.create_auxiliary_artifact(
            execution_id=execution_id,
            label=payload.label,
            content_type=payload.content_type,
            content=payload.content,
        )
    except OperatorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/executions/{execution_id}/artifacts")
def api_execution_artifacts_list(execution_id: str) -> dict:
    """List auxiliary artifacts attached to this execution."""
    service = get_service()
    rec = service.db.get_task_execution(execution_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"unknown execution_id: {execution_id}")
    return {
        "execution_id": execution_id,
        "task_id": rec.task_id,
        "artifacts": service.db.list_artifacts_for_execution(execution_id),
    }


@router.get("/audit")
def api_audit(limit: int = 50, domain: Optional[str] = None, target: Optional[str] = None) -> dict:
    limit = _clamp_limit(limit)
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


@router.get("/recap/awaiting-input")
def api_recap_awaiting_input(domain: Optional[str] = None) -> dict:
    return get_service().recap_awaiting_input(domain=domain)


@router.get("/recap/failures")
def api_recap_failures(domain: Optional[str] = None, limit: int = 20) -> dict:
    return get_service().recap_failures(domain=domain, limit=_clamp_limit(limit))


@router.get("/recap/external-actions")
def api_recap_external_actions(domain: Optional[str] = None, limit: int = 20) -> dict:
    return get_service().recap_external_actions(domain=domain, limit=_clamp_limit(limit))


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
    if isinstance(exc, InvalidTransitionError):
        return HTTPException(status_code=409, detail={"message": str(exc), "details": exc.details})
    if isinstance(exc, OperatorError):
        return HTTPException(status_code=400, detail={"message": str(exc), "details": exc.details})
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.post("/approvals/{approval_id}/approve")
def api_approve(approval_id: str, payload: ApprovalDecisionPayload) -> dict:
    raise HTTPException(
        status_code=403,
        detail="Approval mutations are only available via the dashboard and Discord surfaces.",
    )


@router.post("/approvals/{approval_id}/deny")
def api_deny(approval_id: str, payload: ApprovalDecisionPayload) -> dict:
    raise HTTPException(
        status_code=403,
        detail="Approval mutations are only available via the dashboard and Discord surfaces.",
    )


@router.post("/approvals/{approval_id}/cancel")
def api_cancel(approval_id: str, payload: ApprovalDecisionPayload) -> dict:
    raise HTTPException(
        status_code=403,
        detail="Approval mutations are only available via the dashboard and Discord surfaces.",
    )


@router.post("/executions/callback")
def api_execution_callback(payload: ExecutionCallbackPayload) -> dict:
    """
    Receive agent callback and complete the execution pipeline.

    Dual-shape (detected by presence of `execution_id`):
      - v2: {task_id, execution_id, status, result?, session_key?, metrics?}
        — execution-keyed, structured, first-terminal-wins.
      - v1 (legacy): {task_id, output, session_key?} — output contains
        RESULT_START/RESULT_END + TASK_DONE markers.

    Both paths log `callback_shape_version` in the audit payload for
    deprecation telemetry. Edge cases:
    - Unknown task_id/execution_id: 400
    - Already terminal: 200 { status: already_terminal }
    - Duplicate: 200 { status: success, idempotent: true }
    - Any internal error: 200 { status: error, reason: ... } — never 5xx
    """
    paths = default_paths()

    # v2 path — execution-keyed structured callback
    if payload.execution_id:
        try:
            result = receive_execution_result_v2(
                task_id=payload.task_id,
                execution_id=payload.execution_id,
                status=payload.status or "",
                result=payload.result,
                session_key=payload.session_key,
                metrics=payload.metrics,
                paths=paths,
            )
        except Exception as exc:
            return {
                "status": "error",
                "reason": str(exc),
                "callback_shape_version": "v2",
            }
        if result.task_not_found:
            raise HTTPException(
                status_code=400,
                detail={
                    "status": "error",
                    "reason": result.error or "execution not found",
                    "callback_shape_version": "v2",
                },
            )
        if result.already_terminal:
            return {
                "status": "already_terminal",
                "task_id": result.task_id,
                "task_status": result.terminal_status,
                "idempotent": result.idempotent,
                "callback_shape_version": "v2",
            }
        return {
            "status": "success" if result.success else "error",
            "task_id": result.task_id,
            "artifact_id": result.artifact_id,
            "idempotent": result.idempotent,
            "error": result.error,
            "callback_shape_version": "v2",
        }

    # v1 path — unchanged behavior
    if payload.output is None:
        raise HTTPException(
            status_code=422,
            detail={
                "status": "error",
                "reason": "v1 callback requires `output`; v2 callback requires `execution_id`",
            },
        )
    if len(payload.output.encode("utf-8", errors="replace")) > _MAX_CALLBACK_OUTPUT_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "status": "error",
                "reason": f"output exceeds maximum size ({_MAX_CALLBACK_OUTPUT_BYTES // (1024*1024)} MB)",
            },
        )
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
                "callback_shape_version": "v1",
            }
        return {
            "status": "success" if result.success else "error",
            "task_id": result.task_id,
            "artifact_id": result.artifact_id,
            "idempotent": result.idempotent,
            "error": result.error,
            "callback_shape_version": "v1",
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
        return asdict(get_service().operator_close_task(task_id, reason=payload.reason))
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
