from __future__ import annotations

import json

from .models import TaskRecord


EMAIL_DRAFT_ARTIFACT_TYPE = "email_draft"


def build_email_draft_brief(
    *,
    task_id: str,
    subject: str,
    sender: str,
    summary: str | None,
) -> str:
    clean_summary = summary or "(No summary provided)"
    return (
        "You are drafting an email reply on behalf of Dara.\n\n"
        "Original email:\n"
        f"  Subject: {subject}\n"
        f"  From: {sender}\n"
        f"  Summary: {clean_summary}\n\n"
        f"Task ID: {task_id}\n\n"
        "Instructions:\n"
        "- Draft a reply that Dara would approve. Tone: direct, professional, concise.\n"
        "- Do NOT include greetings or sign-offs unless clearly appropriate.\n"
        "- Do NOT include any personal information not in the original summary.\n"
        "- Output format:\n"
        "  RESULT_START\n"
        "  {draft reply text only, no metadata}\n"
        "  RESULT_END\n"
        f"  TASK_DONE: {task_id}\n"
    )


def build_email_draft_brief_for_task(task: TaskRecord) -> str:
    metadata: dict[str, str | None] = {}
    if task.request_metadata_json:
        try:
            loaded = json.loads(task.request_metadata_json)
            if isinstance(loaded, dict):
                metadata = {str(key): _optional_text(value) for key, value in loaded.items()}
        except json.JSONDecodeError:
            metadata = {}
    subject = metadata.get("subject") or task.user_request.splitlines()[0]
    sender = metadata.get("sender") or "Unknown sender"
    summary = metadata.get("summary")
    return build_email_draft_brief(
        task_id=task.id,
        subject=subject,
        sender=sender,
        summary=summary,
    )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
