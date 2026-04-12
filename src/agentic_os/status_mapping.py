from __future__ import annotations

from .models import validate_choice

PAPERCLIP_STATUSES = (
    "backlog",
    "todo",
    "in_review",
    "blocked",
    "in_progress",
    "done",
    "cancelled",
)

PAPERCLIP_TO_BACKEND_STATUS: dict[str, str] = {
    "backlog": "to_do",
    "todo": "to_do",
    "in_review": "to_do",
    "blocked": "to_do",
    "in_progress": "in_progress",
    "done": "done",
    "cancelled": "done",
}


def map_paperclip_status_to_backend(paperclip_status: str) -> str:
    status = (paperclip_status or "").strip().lower()
    validate_choice(status, PAPERCLIP_STATUSES, "paperclip_status")
    return PAPERCLIP_TO_BACKEND_STATUS[status]
