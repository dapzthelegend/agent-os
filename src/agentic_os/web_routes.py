from __future__ import annotations

from dataclasses import asdict
from typing import Callable, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .models import OperatorError
from .health import get_system_health
from .web_support import (
    annotate_audit_events,
    approval_groups,
    build_overview,
    enrich_approval_detail,
    enrich_task_detail,
    format_json,
    get_service,
    parse_artifact_input,
    task_filter_options,
)


router = APIRouter(tags=["html"])


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _render(request: Request, template_name: str, context: dict, status_code: int = 200):
    full_context = {
        "request": request,
        "page": template_name,
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
        **context,
    }
    return _templates(request).TemplateResponse(template_name, full_context, status_code=status_code)


def _handle_page_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, OperatorError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _redirect(url: str, *, message: Optional[str] = None, error: Optional[str] = None) -> RedirectResponse:
    query = {}
    if message:
        query["message"] = message
    if error:
        query["error"] = error
    target = f"{url}?{urlencode(query)}" if query else url
    return RedirectResponse(target, status_code=303)


def _apply_approval(
    action: Callable[..., dict],
    approval_id: str,
    note: Optional[str],
    redirect_to: str,
) -> RedirectResponse:
    try:
        action(approval_id, decision_note=note or None)
    except Exception as exc:
        return _redirect(redirect_to, error=str(exc))
    return _redirect(redirect_to, message=f"Approval {approval_id} updated.")


@router.get("/")
def overview(request: Request):
    return _render(request, "overview.html", {"overview": build_overview(get_service())})


@router.get("/tasks")
def tasks_page(
    request: Request,
    limit: int = 50,
    status: Optional[str] = None,
    domain: Optional[str] = None,
    target: Optional[str] = None,
    action_source: Optional[str] = None,
):
    service = get_service()
    tasks = service.list_tasks(
        limit=limit,
        status=status,
        domain=domain,
        target=target,
        action_source=action_source,
    )
    return _render(
        request,
        "tasks.html",
        {
            "tasks": [asdict(task) for task in tasks],
            "filters": {
                "limit": limit,
                "status": status or "",
                "domain": domain or "",
                "target": target or "",
                "action_source": action_source or "",
            },
            "filter_options": task_filter_options(service),
        },
    )


@router.get("/tasks/{task_id}")
def task_detail(request: Request, task_id: str, artifact_id: Optional[str] = None):
    try:
        detail = enrich_task_detail(get_service(), task_id, artifact_id=artifact_id)
    except Exception as exc:
        raise _handle_page_error(exc) from exc
    detail["audit_events"] = annotate_audit_events(detail["audit_events"])
    detail["request_metadata_pretty"] = (
        format_json(detail["request_metadata"]) if detail["request_metadata"] is not None else None
    )
    return _render(request, "task_detail.html", detail)


@router.post("/tasks/{task_id}/artifacts/revise")
def revise_artifact(
    task_id: str,
    artifact_type: str = Form(default=""),
    artifact_text: str = Form(default=""),
    artifact_json: str = Form(default=""),
):
    try:
        artifact_content = parse_artifact_input(artifact_text, artifact_json)
        get_service().revise_artifact(
            task_id,
            artifact_type=artifact_type or None,
            artifact_content=artifact_content,
        )
    except Exception as exc:
        return _redirect(f"/tasks/{task_id}", error=str(exc))
    return _redirect(f"/tasks/{task_id}", message=f"Artifact revised for {task_id}.")


@router.get("/approvals")
def approvals_page(request: Request):
    service = get_service()
    groups = approval_groups(service)
    details = [enrich_approval_detail(service, approval["id"]) for approval in groups["pending"]]
    return _render(
        request,
        "approvals.html",
        {
            "groups": groups,
            "pending_details": details,
        },
    )


@router.get("/approvals/{approval_id}")
def approval_detail(request: Request, approval_id: str):
    try:
        detail = enrich_approval_detail(get_service(), approval_id)
    except Exception as exc:
        raise _handle_page_error(exc) from exc
    detail["approval_payload_pretty"] = format_json(detail["approval_payload"])
    return _render(request, "approval_detail.html", detail)


@router.post("/approvals/{approval_id}/approve")
def approve_approval(approval_id: str, note: str = Form(default=""), redirect_to: str = Form(default="/approvals")):
    return _apply_approval(get_service().approve, approval_id, note, redirect_to)


@router.post("/approvals/{approval_id}/deny")
def deny_approval(approval_id: str, note: str = Form(default=""), redirect_to: str = Form(default="/approvals")):
    return _apply_approval(get_service().deny, approval_id, note, redirect_to)


@router.post("/approvals/{approval_id}/cancel")
def cancel_approval(approval_id: str, note: str = Form(default=""), redirect_to: str = Form(default="/approvals")):
    return _apply_approval(get_service().cancel, approval_id, note, redirect_to)


@router.get("/executions/{operation_key}")
def execution_detail(request: Request, operation_key: str):
    try:
        detail = get_service().get_execution_detail(operation_key)
    except Exception as exc:
        raise _handle_page_error(exc) from exc
    if detail.get("audit_events"):
        detail["audit_events"] = annotate_audit_events(detail["audit_events"])
    return _render(request, "execution_detail.html", detail)


@router.get("/audit")
def audit_page(request: Request, limit: int = 50, domain: Optional[str] = None, target: Optional[str] = None):
    service = get_service()
    payload = service.list_recent_audit_activity(limit=limit, domain=domain, target=target)
    payload["events"] = annotate_audit_events(payload["events"])
    return _render(
        request,
        "audit.html",
        {
            "audit": payload,
            "filter_options": task_filter_options(service),
        },
    )


@router.get("/health")
def health_page(request: Request):
    service = get_service()
    health = get_system_health(service)
    return _render(request, "health.html", {"health": health})


@router.get("/stalled")
def stalled_page(request: Request):
    service = get_service()
    from .recovery import find_stalled_tasks
    tasks = find_stalled_tasks(service, threshold_hours=2.0)
    return _render(request, "stalled.html", {"stalled_tasks": tasks, "threshold_hours": 2.0})


@router.post("/stalled/{task_id}/retry")
def retry_stalled(task_id: str, feedback: str = Form(default="operator retry")):
    try:
        get_service().retry_task(task_id, feedback=feedback)
    except Exception as exc:
        return _redirect("/stalled", error=str(exc))
    return _redirect("/stalled", message=f"Task {task_id} reset for retry.")


@router.post("/tasks/{task_id}/approve-plan")
def approve_plan(task_id: str, revision_id: str = Form(default="")):
    try:
        service = get_service()
        if not revision_id:
            task = service.db.get_task(task_id)
            revision_id = f"plan-v{task.plan_version or 1}-operator"
        service.approve_plan(task_id, revision_id=revision_id)
    except Exception as exc:
        return _redirect(f"/tasks/{task_id}", error=str(exc))
    return _redirect(f"/tasks/{task_id}", message=f"Plan approved ({revision_id}).")


@router.post("/tasks/{task_id}/reject-plan")
def reject_plan(task_id: str, feedback: str = Form(default="")):
    try:
        get_service().reject_plan(task_id, feedback=feedback or "Revision requested by operator")
    except Exception as exc:
        return _redirect(f"/tasks/{task_id}", error=str(exc))
    return _redirect(f"/tasks/{task_id}", message=f"Plan rejected for task {task_id}.")


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, reason: str = Form(default="Cancelled by operator")):
    try:
        get_service().cancel_task(task_id, reason=reason)
    except Exception as exc:
        return _redirect(f"/tasks/{task_id}", error=str(exc))
    return _redirect(f"/tasks/{task_id}", message=f"Task {task_id} cancelled.")


@router.post("/tasks/{task_id}/set-mode")
def set_task_mode(task_id: str, mode: str = Form(...)):
    try:
        get_service().set_task_mode(task_id, mode=mode)
    except Exception as exc:
        return _redirect(f"/tasks/{task_id}", error=str(exc))
    return _redirect(f"/tasks/{task_id}", message=f"Task mode set to '{mode}'.")


@router.get("/paperclip")
def paperclip_page(request: Request):
    from .health import get_paperclip_health
    health = get_paperclip_health(get_service())
    return _render(request, "paperclip.html", {"paperclip": health})


@router.get("/recaps")
def recaps_page(request: Request, domain: Optional[str] = None):
    service = get_service()
    return _render(
        request,
        "recaps.html",
        {
            "domain": domain or "",
            "recap_today": service.recap_today(domain=domain),
            "recap_approvals": service.recap_approvals(domain=domain),
            "recap_drafts": service.recap_drafts(domain=domain),
            "recap_failures": service.recap_failures(domain=domain, limit=10),
            "recap_external_actions": service.recap_external_actions(domain=domain, limit=10),
            "domains": [""] + list(task_filter_options(service)["domains"]),
        },
    )
