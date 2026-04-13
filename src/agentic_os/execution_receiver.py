"""Execution receiver — parses ACP output, stores artifact, updates backend + Paperclip."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from typing import Optional

from .artifacts import ArtifactStore
from .config import Paths, default_paths, load_app_config
from .github_contribution import parse_github_contribution_result
from .models import TaskRecord
from .service import AgenticOSService


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _looks_like_uuid(s: str) -> bool:
    """Check if a string looks like a UUID (prevents emergency import on garbage inputs)."""
    return bool(s and _UUID_RE.match(s.strip()))


class ExecutionParseError(Exception):
    """Raised when ACP output cannot be parsed."""
    pass


@dataclass(frozen=True)
class ExecutionResult:
    """Result of receiving an execution."""
    task_id: str
    artifact_id: str
    success: bool
    idempotent: bool = False
    error: Optional[str] = None
    task_not_found: bool = False
    already_terminal: bool = False
    terminal_status: Optional[str] = None


def _extract_paperclip_run_id(session_key: str) -> Optional[str]:
    """
    Extract Paperclip routine run id from session key format:
    paperclip:run:<run_id>
    """
    if not session_key:
        return None
    parts = [segment.strip() for segment in session_key.split(":")]
    if len(parts) < 3:
        return None
    if parts[0] != "paperclip" or parts[1] != "run":
        return None
    run_id = parts[2]
    return run_id or None


def _import_task_from_paperclip_issue(
    *,
    service: AgenticOSService,
    issue_id: str,
    session_key: str,
) -> Optional[TaskRecord]:
    """
    Best-effort emergency import when callback references an issue not yet in backend.
    """
    cp = service._cp
    if cp is None or not issue_id:
        return None

    issue = cp.get_issue(issue_id)
    if issue is None or not issue.id:
        return None

    run_id = _extract_paperclip_run_id(session_key)
    origin_kind = "routine_execution" if run_id else "manual_issue"

    try:
        imported = service.import_paperclip_issue(
            issue_id=issue.id,
            title=issue.title or issue.id,
            description="",
            paperclip_status=issue.status or "backlog",
            project_id=issue.project_id,
            routine_run_id=run_id,
            origin_kind=origin_kind,
        )
    except Exception:
        return None

    task = imported.get("task")
    return task if isinstance(task, TaskRecord) else None


def _artifact_type_for_domain(domain: str, intent_type: str) -> str:
    """Determine artifact type from task domain and intent."""
    if intent_type == "content":
        return "content_markdown"
    elif domain == "personal" and intent_type == "draft":
        return "draft"
    elif domain == "technical" and intent_type == "execute":
        return "code"
    elif domain == "technical" and intent_type == "read":
        return "research_summary"
    else:
        return "output"


def receive_execution_result(
    raw_output: str,
    *,
    task_id: str,
    session_key: str,
    paths: Paths,
) -> ExecutionResult:
    """
    Parse ACP agent output, store artifact, update backend + Paperclip.
    
    Args:
        raw_output: Full output from ACP agent
        task_id: Backend task ID
        session_key: OpenClaw session key (for audit)
        paths: Paths config
    
    Returns:
        ExecutionResult with artifact_id and success status
    
    Raises:
        ExecutionParseError: if output cannot be parsed
    """
    try:
        # Step 1: Parse output
        if "RESULT_START" not in raw_output or "RESULT_END" not in raw_output:
            raise ExecutionParseError("Missing RESULT_START/RESULT_END markers")

        start_idx = raw_output.find("RESULT_START") + len("RESULT_START")
        end_idx = raw_output.find("RESULT_END")
        content = raw_output[start_idx:end_idx].strip()

        # Verify TASK_DONE marker
        if f"TASK_DONE: {task_id}" not in raw_output:
            raise ExecutionParseError(f"Missing or mismatched TASK_DONE marker for {task_id}")

        # Step 2: Load backend task — structured resolution
        config = load_app_config(paths)
        service = AgenticOSService(paths, config)

        task = None
        resolved_task_id = task_id

        # Primary: backend task_id format (task_XXXXXX)
        if task_id and task_id.startswith("task_"):
            try:
                task = service.db.get_task(task_id)
            except KeyError:
                pass
            # If a task_ prefixed ID wasn't found in DB, try session key fallback
            if task is None and session_key:
                task = service.db.get_task_by_dispatch_session_key(session_key)
                if task is not None:
                    resolved_task_id = task.id

        # Secondary: caller passed a Paperclip ID or other non-task_ identifier
        if task is None and task_id and not task_id.startswith("task_"):
            task = service.db.get_task_by_paperclip_issue_id(task_id)
            if task is not None:
                resolved_task_id = task.id

        if task is None and task_id and not task_id.startswith("task_"):
            task = service.db.get_task_by_paperclip_routine_run_id(task_id)
            if task is not None:
                resolved_task_id = task.id

        if task is None and session_key:
            task = service.db.get_task_by_dispatch_session_key(session_key)
            if task is not None:
                resolved_task_id = task.id

        # Emergency import — only for UUID-format IDs (prevents firing on garbage)
        if task is None and _looks_like_uuid(task_id):
            imported_task = _import_task_from_paperclip_issue(
                service=service,
                issue_id=task_id,
                session_key=session_key,
            )
            if imported_task is not None:
                task = imported_task
                resolved_task_id = imported_task.id

        if task is None:
            return ExecutionResult(
                task_id=task_id,
                artifact_id="",
                success=False,
                task_not_found=True,
                error="task not found",
            )

        task_id = resolved_task_id

        # Step 2b: Terminal status check — do not re-process done tasks.
        if task.status == "done":
            service._append_event(
                task_id=task_id,
                event_type="action_execution_rejected",
                payload={
                    "operation_key": task.operation_key,
                    "reason": "already_done_for_task",
                    "task_status": task.status,
                },
            )
            return ExecutionResult(
                task_id=task_id,
                artifact_id=task.artifact_ref or "",
                success=True,
                idempotent=True,
            )
        
        # Step 3: Determine artifact type
        artifact_type = _artifact_type_for_domain(task.domain, task.intent_type)
        
        # Step 4: Store artifact on disk
        artifact_store = ArtifactStore(paths.artifacts_dir)
        artifact_record = artifact_store.write(
            task_id=task_id,
            artifact_type=artifact_type,
            content=content,
        )
        artifact_id = artifact_record.id

        # Step 4b: Register artifact in DB so it appears in list/query endpoints
        service.db.insert_artifact(
            artifact_id=artifact_record.id,
            task_id=artifact_record.task_id,
            artifact_type=artifact_record.artifact_type,
            path=artifact_record.path,
            version=artifact_record.version,
            content_preview=artifact_record.content_preview,
            created_at=artifact_record.created_at,
        )

        # Step 5: Update backend task (no Paperclip writeback — the runtime
        # posts canonical comments; the backend cannot safely comment on
        # open issues, see TaskControlPlane comment block).
        parsed_contribution = parse_github_contribution_result(content)
        summary_source = parsed_contribution.content_without_block if parsed_contribution else content
        result_summary = summary_source[:200] if summary_source else "Execution completed"
        if parsed_contribution is not None:
            service.record_github_contribution_result(
                task_id,
                result_summary=result_summary,
                artifact_ref=artifact_id,
                contribution=parsed_contribution.payload,
            )
        else:
            service.complete_task(
                task_id,
                result_summary=result_summary,
                artifact_ref=artifact_id,
            )

        # Step 5b: Emit callback audit event with full correlation IDs
        reloaded_task = service.db.get_task(task_id)
        execution_record = service.db.get_execution(reloaded_task.operation_key) if reloaded_task.operation_key else None
        approval_id = execution_record.approval_id if execution_record else None
        service._append_event(
            task_id=task_id,
            event_type="execution_callback_received",
            payload={
                "task_id": task_id,
                "execution_id": reloaded_task.operation_key or "",
                "approval_id": approval_id,
                "session_key": session_key,
                "artifact_id": artifact_id,
                "result_summary": result_summary,
            },
        )

        # Step 6: Return success
        return ExecutionResult(task_id=task_id, artifact_id=artifact_id, success=True)
    
    except ExecutionParseError:
        raise
    except Exception as e:
        return ExecutionResult(
            task_id=task_id,
            artifact_id="",
            success=False,
            error=str(e),
        )


def receive_execution_failure(
    error: str,
    *,
    task_id: str,
    session_key: str = "unknown",
    paths: Paths,
) -> None:
    """
    Handle execution failure by updating backend state and audit.
    
    Args:
        error: Error message from agent
        task_id: Backend task ID
        paths: Paths config
    """
    try:
        config = load_app_config(paths)
        service = AgenticOSService(paths, config)
        task = service.db.get_task(task_id)

        # Update backend
        service.fail_task(task_id, reason=error)

        # Trigger self-healing incident follow-up task for runtime failures.
        try:
            service.create_runtime_incident_task(
                summary="Execution callback failure requires runtime remediation",
                origin_task_id=task_id,
                origin_session_key=session_key,
                runtime_id=task.claimed_by,
                component="execution_callback",
                error_type="execution_failure",
                error_message=error,
            )
        except Exception:
            pass
    
    except Exception as e:
        print(f"Error in receive_execution_failure: {e}", file=sys.stderr)


def main() -> int:
    """CLI entry point: read JSON from stdin, process execution result."""
    try:
        data = json.loads(sys.stdin.read())
        task_id = data["task_id"]
        session_key = data.get("session_key", "unknown")
        raw_output = data["output"]
        
        paths = default_paths()
        result = receive_execution_result(
            raw_output,
            task_id=task_id,
            session_key=session_key,
            paths=paths,
        )
        
        if result.success:
            print(json.dumps({
                "status": "success",
                "task_id": result.task_id,
                "artifact_id": result.artifact_id,
            }))
            return 0
        else:
            print(json.dumps({
                "status": "error",
                "task_id": result.task_id,
                "error": result.error,
            }), file=sys.stderr)
            return 1
    
    except Exception as e:
        print(json.dumps({
            "status": "error",
            "error": str(e),
        }), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
