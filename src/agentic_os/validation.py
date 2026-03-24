"""Validation gate — Senior (Opus) agent validation for high-risk tasks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import Paths, load_app_config
from .models import TaskRecord
from .service import AgenticOSService


@dataclass(frozen=True)
class ValidationResult:
    """Result of validation."""
    task_id: str
    passed: bool
    reason: str
    feedback: Optional[str] = None


def needs_validation(task: TaskRecord) -> bool:
    """
    Determine if a task needs validation before completion.
    
    Args:
        task: TaskRecord to evaluate
    
    Returns:
        True if validation is required
    """
    # High risk always needs validation
    if task.risk_level == "high":
        return True
    
    # Finance always needs validation
    if task.domain == "finance":
        return True
    
    # Medium+ risk + external write needs validation
    if task.risk_level == "medium" and task.external_write:
        return True
    
    return False


def build_validation_prompt(task: TaskRecord, artifact_content: str) -> str:
    """
    Build validation prompt for Senior (Opus) agent.
    
    Args:
        task: TaskRecord to validate
        artifact_content: Content to validate
    
    Returns:
        Validation prompt string
    """
    truncated_content = artifact_content[:3000]
    
    return f"""You are a senior validator. Evaluate whether this task output is complete and correct.

Original task: {task.user_request}
Domain: {task.domain} | Intent: {task.intent_type} | Risk: {task.risk_level}
Target: {task.target or 'none'}

Output to validate:
{truncated_content}

Respond with exactly one of:
VALIDATION_PASS: <one-line reason>
VALIDATION_FAIL: <specific issue that needs fixing>

Do not add any other text."""


def handle_validation_verdict(
    verdict_text: str,
    *,
    task_id: str,
    artifact_id: str,
    paths: Paths,
) -> ValidationResult:
    """
    Process validation verdict from Senior agent.
    
    Args:
        verdict_text: Verdict from Opus agent
        task_id: Backend task ID
        artifact_id: Artifact being validated
        paths: Paths config
    
    Returns:
        ValidationResult with outcome
    """
    config = load_app_config(paths)
    service = AgenticOSService(config, paths)
    task = service.db.get_task(task_id)
    
    if verdict_text.startswith("VALIDATION_PASS"):
        # Extract reason
        reason = verdict_text[len("VALIDATION_PASS:"):].strip()
        
        # Complete the task (same as execution_receiver step 5+)
        service.complete_task(
            task_id,
            result_summary=f"Validated: {reason[:100]}",
            artifact_ref=artifact_id,
        )
        
        service._append_event(
            task_id=task_id,
            event_type="validation_passed",
            payload={"reason": reason, "artifact_id": artifact_id},
        )
        
        return ValidationResult(
            task_id=task_id,
            passed=True,
            reason=reason,
        )
    
    elif verdict_text.startswith("VALIDATION_FAIL"):
        # Extract feedback
        feedback = verdict_text[len("VALIDATION_FAIL:"):].strip()
        
        # Reset for retry if retry_count < 2
        retry_result = service.reset_task_for_retry(task_id, feedback=feedback)
        
        service._append_event(
            task_id=task_id,
            event_type="validation_failed",
            payload={
                "feedback": feedback,
                "artifact_id": artifact_id,
                "retry_count": retry_result.retry_count,
            },
        )
        
        return ValidationResult(
            task_id=task_id,
            passed=False,
            reason="Validation failed",
            feedback=feedback,
        )
    
    else:
        # Unclear verdict
        service._append_event(
            task_id=task_id,
            event_type="validation_unclear",
            payload={"verdict_text": verdict_text},
        )
        
        return ValidationResult(
            task_id=task_id,
            passed=False,
            reason="Validation response unclear",
            feedback=verdict_text,
        )
