from __future__ import annotations

from .models import TaskRecord


APPROVAL_RECIPIENT = "franchieinc@gmail.com"


def build_approval_email(task: TaskRecord, artifact_content: str | None) -> dict[str, str]:
    task_title = task.user_request.splitlines()[0].strip() or task.user_request
    subject = f"[Agent] Approval needed: {task_title}"

    lines = [
        f"Task: {task_title}",
        f"ID: {task.id}",
        f"Domain: {task.domain} | Risk: {task.risk_level}",
        f"Created: {task.created_at}",
        "",
    ]
    if artifact_content:
        lines.extend(
            [
                "Draft:",
                "---",
                artifact_content[:1000],
                "---",
                "",
            ]
        )
    lines.extend(
        [
            f'To approve: reply "approve {task.id}"',
            f'To reject:  reply "reject {task.id} <reason>"',
        ]
    )

    return {
        "subject": subject,
        "body": "\n".join(lines),
        "to": APPROVAL_RECIPIENT,
    }
