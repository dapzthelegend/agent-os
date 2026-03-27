"""Execution receiver — parses ACP output, stores artifact, updates backend + Paperclip."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Optional

from .artifacts import ArtifactStore
from .config import Paths, default_paths, load_app_config
from .email_draft_handler import EMAIL_DRAFT_ARTIFACT_TYPE
from .models import TaskRecord
from .service import AgenticOSService


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


def _artifact_type_for_domain(domain: str, intent_type: str) -> str:
    """Determine artifact type from task domain and intent."""
    if intent_type == "content":
        return "content_markdown"
    elif domain == "personal" and intent_type == "draft":
        return EMAIL_DRAFT_ARTIFACT_TYPE
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
    Parse ACP agent output, store artifact, update backend + Notion.
    
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

        # Step 2: Load backend task — with session_key fallback
        config = load_app_config(paths)
        service = AgenticOSService(paths, config)

        task = None
        resolved_task_id = task_id
        if task_id:
            try:
                task = service.db.get_task(task_id)
            except KeyError:
                pass

        if task is None:
            # Fallback: look up by dispatch_session_key
            task = service.db.get_task_by_dispatch_session_key(session_key)
            if task is not None:
                resolved_task_id = task.id

        if task is None:
            return ExecutionResult(
                task_id=task_id,
                artifact_id="",
                success=False,
                task_not_found=True,
                error="task not found",
            )

        task_id = resolved_task_id

        # Step 2b: Terminal status check — do not re-process terminal tasks.
        #
        # completed/executed: task was already successfully handled by the execution
        # receiver (duplicate callback) — return idempotent so the caller can safely
        # ignore the response.
        # failed/cancelled: task reached a non-success terminal state; block re-processing
        # and surface the terminal status explicitly.
        if task.status in {"completed", "executed"}:
            service._append_event(
                task_id=task_id,
                event_type="action_execution_rejected",
                payload={
                    "operation_key": task.operation_key,
                    "reason": "already_executed_for_task",
                    "task_status": task.status,
                },
            )
            return ExecutionResult(
                task_id=task_id,
                artifact_id=task.artifact_ref or "",
                success=True,
                idempotent=True,
            )
        if task.status in {"failed", "cancelled"}:
            return ExecutionResult(
                task_id=task_id,
                artifact_id=task.artifact_ref or "",
                success=True,
                already_terminal=True,
                terminal_status=task.status,
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

        # Step 5: Update backend task + Paperclip writeback
        result_summary = content[:200] if content else "Execution completed"
        service.complete_task(
            task_id,
            result_summary=result_summary,
            artifact_ref=artifact_id,
            paperclip_content=content,
            artifact_path=artifact_record.path,
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
    paths: Paths,
) -> None:
    """
    Handle execution failure: update backend and Notion.
    
    Args:
        error: Error message from agent
        task_id: Backend task ID
        paths: Paths config
    """
    try:
        config = load_app_config(paths)
        service = AgenticOSService(paths, config)
        task = service.db.get_task(task_id)

        # Update backend (Paperclip writeback handled inside fail_task)
        service.fail_task(task_id, reason=error)
    
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
