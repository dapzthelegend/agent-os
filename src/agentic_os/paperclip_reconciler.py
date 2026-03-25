"""
Paperclip reconciler — reflects operator actions from Paperclip into backend state.

Polls company-level activity, then for each unseen event on a known issue:

  Comment events
  ─────────────
  Approval signals  (APPROVE / LGTM / APPROVED)
    → approve_plan() if task is awaiting_plan_review

  Revision signals  (REVISE: / REVISION: / REQUEST_REVISION:)
    → reject_plan() if task is awaiting_plan_review

  Ambiguous comments → logged only (reconciler_comment_ignored)

  Status-change events
  ────────────────────
  new status = cancelled
    → cancel_task() unless already terminal

Design notes
  - Idempotent: seen event IDs are persisted in a JSON state file.
  - Failure-tolerant: every per-event dispatch is wrapped; one failure
    cannot abort the rest of the run.
  - No Paperclip writeback on reconciler-sourced cancellations (avoids loops).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from .config import AppConfig, Paths
from .paperclip_client import ActivityEvent
from .service import AgenticOSService
from .task_control_plane import TaskControlPlane

log = logging.getLogger(__name__)

# Approval comment signals (checked case-insensitively against the full body
# or as a word/prefix).
_APPROVAL_SIGNALS = ("approve", "lgtm", "approved")

# Revision-request prefixes (checked case-insensitively against body start).
_REVISION_PREFIXES = ("revise:", "revision:", "request_revision:")

# Maximum number of seen event IDs to retain (rolling window).
_MAX_SEEN = 2000

# Paperclip activity event types we care about.
_COMMENT_EVENT_TYPES = {"comment_added", "comment", "comment_created"}
_STATUS_EVENT_TYPES = {"status_changed", "status_change", "issue_status_changed"}


class PaperclipReconciler:
    """
    Single-pass reconciler: call run_once() periodically (e.g. every 2 min).
    """

    def __init__(self, paths: Paths, config: AppConfig) -> None:
        self._paths = paths
        self._config = config
        self._service = AgenticOSService(paths, config)
        self._cp: Optional[TaskControlPlane] = None
        if config.paperclip is not None:
            self._cp = TaskControlPlane(config.paperclip)
        self._state_path = paths.data_dir / "paperclip_reconciler_state.json"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_once(self) -> dict[str, Any]:
        """
        Poll Paperclip activity and reconcile operator actions.

        Returns a summary dict:
            {events_polled, actions_taken, errors, skipped}
        """
        if self._cp is None:
            return {"events_polled": 0, "actions_taken": 0, "errors": 0, "skipped": 0,
                    "note": "paperclip not configured"}

        seen = self._load_seen()
        actions_taken = 0
        errors = 0
        skipped = 0

        try:
            events = self._cp.poll_company_activity()
        except Exception as exc:
            log.error("reconciler: poll_company_activity failed: %s", exc)
            return {"events_polled": 0, "actions_taken": 0, "errors": 1, "skipped": 0}

        for event in events:
            if event.id in seen:
                skipped += 1
                continue
            try:
                action = self._dispatch(event)
                if action:
                    actions_taken += 1
                    log.info("reconciler: action=%s issue=%s event=%s", action, event.issue_id, event.id)
            except Exception as exc:
                errors += 1
                log.error("reconciler: dispatch error for event %s: %s", event.id, exc)
            finally:
                seen.add(event.id)

        self._save_seen(seen)

        # Emit a single audit event summarising the run (best-effort, no task_id needed
        # but the audit API requires one — use a sentinel).
        try:
            self._service._append_event(
                task_id="__reconciler__",
                event_type="reconciler_ran",
                payload={
                    "events_polled": len(events),
                    "actions_taken": actions_taken,
                    "errors": errors,
                    "skipped": skipped,
                },
            )
        except Exception:
            pass

        return {
            "events_polled": len(events),
            "actions_taken": actions_taken,
            "errors": errors,
            "skipped": skipped,
        }

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, event: ActivityEvent) -> Optional[str]:
        """Route an activity event to the appropriate handler. Returns action name or None."""
        task = self._service.db.get_task_by_paperclip_issue_id(event.issue_id)
        if task is None:
            return None  # not a tracked issue

        et = event.event_type.lower()

        if et in _COMMENT_EVENT_TYPES:
            return self._handle_comment(task, event)
        if et in _STATUS_EVENT_TYPES:
            return self._handle_status_change(task, event)

        return None

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_comment(self, task, event: ActivityEvent) -> Optional[str]:
        body: str = ""
        payload = event.payload or {}
        # Paperclip may put the comment body under different keys
        for key in ("body", "text", "content", "comment"):
            if key in payload:
                body = str(payload[key])
                break

        body_stripped = body.strip()
        body_lower = body_stripped.lower()

        # --- Approval signals ---
        if any(body_lower == sig or body_lower.startswith(sig + " ") for sig in _APPROVAL_SIGNALS):
            if task.status == "awaiting_plan_review":
                revision_id = f"plan-v{task.plan_version}-reconciler"
                self._service.approve_plan(task.id, revision_id=revision_id)
                self._emit_action(task.id, "plan_approved", event, {"revision_id": revision_id})
                return "plan_approved"
            else:
                self._emit_ignored(task.id, event, body_stripped, "approval signal but task not in awaiting_plan_review")
                return None

        # --- Revision signals ---
        if any(body_lower.startswith(prefix) for prefix in _REVISION_PREFIXES):
            if task.status == "awaiting_plan_review":
                self._service.reject_plan(task.id, feedback=body_stripped)
                self._emit_action(task.id, "plan_rejected", event, {"feedback": body_stripped})
                return "plan_rejected"
            else:
                self._emit_ignored(task.id, event, body_stripped, "revision signal but task not in awaiting_plan_review")
                return None

        # --- Ambiguous ---
        return None

    def _handle_status_change(self, task, event: ActivityEvent) -> Optional[str]:
        payload = event.payload or {}
        new_status: str = str(
            payload.get("status") or payload.get("newStatus") or payload.get("new_status") or ""
        ).lower()

        if new_status == "cancelled":
            if task.status not in {"cancelled", "completed", "failed"}:
                self._service.cancel_task(task.id, reason="Cancelled by operator in Paperclip")
                self._emit_action(task.id, "task_cancelled", event, {})
                return "task_cancelled"

        return None

    # ------------------------------------------------------------------
    # Audit helpers
    # ------------------------------------------------------------------

    def _emit_action(
        self, task_id: str, action: str, event: ActivityEvent, extra: dict
    ) -> None:
        try:
            self._service._append_event(
                task_id=task_id,
                event_type="reconciler_action_taken",
                payload={"action": action, "paperclip_event_id": event.id, **extra},
            )
        except Exception:
            pass

    def _emit_ignored(
        self, task_id: str, event: ActivityEvent, body: str, reason: str
    ) -> None:
        try:
            self._service._append_event(
                task_id=task_id,
                event_type="reconciler_comment_ignored",
                payload={
                    "reason": reason,
                    "paperclip_event_id": event.id,
                    "body_preview": body[:120],
                },
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # State persistence (seen event IDs)
    # ------------------------------------------------------------------

    def _load_seen(self) -> set[str]:
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                return set(data.get("seen", []))
        except Exception as exc:
            log.warning("reconciler: could not load state file: %s", exc)
        return set()

    def _save_seen(self, seen: set[str]) -> None:
        try:
            # Keep only the most recent _MAX_SEEN IDs (sets are unordered;
            # we just trim to size to prevent unbounded growth).
            trimmed = list(seen)
            if len(trimmed) > _MAX_SEEN:
                trimmed = trimmed[-_MAX_SEEN:]
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps({"seen": trimmed}, indent=None),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("reconciler: could not save state file: %s", exc)


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

def main() -> int:
    """Run one reconciler pass. Exit 0 on success, 1 on hard failure."""
    import sys
    from .config import default_paths, load_app_config

    paths = default_paths()
    try:
        config = load_app_config(paths)
    except Exception as exc:
        print(f"reconciler: config load failed: {exc}", file=sys.stderr)
        return 1

    reconciler = PaperclipReconciler(paths, config)
    result = reconciler.run_once()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
