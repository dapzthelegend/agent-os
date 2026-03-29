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

  Issue-created events (Paperclip-originated tasks)
  ──────────────────────────────────────────────────
  issue_created / new_issue / issue_added for an untracked issue
    → import_paperclip_issue() — creates a backend task linked to the
      existing Paperclip issue; assigns the appropriate agent; writes
      the callback brief document; lifecycle proceeds as normal.

  Safety-net scan (scan_untracked_issues)
  ───────────────────────────────────────
  Scans all open Paperclip issues and imports any that have no matching
  backend task.  Called at the end of every run_once() to catch issues
  created before the reconciler started, or missed during a gap.

Design notes
  - Idempotent: seen event IDs are persisted in a JSON state file.
    Imported issue IDs are persisted in the same file under "imported_issues".
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
_ISSUE_CREATED_EVENT_TYPES = {"issue_created", "new_issue", "issue_added"}
_ROUTINE_ENTITY_TYPES = {"routine", "routine_trigger", "routine_run"}
_ROUTINE_NON_TASK_TERMINAL_STATUSES = {"coalesced", "skipped"}


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
            {events_polled, actions_taken, errors, skipped, imported}
        """
        if self._cp is None:
            return {"events_polled": 0, "actions_taken": 0, "errors": 0, "skipped": 0,
                    "imported": 0, "note": "paperclip not configured"}

        seen, imported_issues = self._load_state()
        actions_taken = 0
        errors = 0
        skipped = 0
        imported = 0

        try:
            events = self._cp.poll_company_activity()
        except Exception as exc:
            log.error("reconciler: poll_company_activity failed: %s", exc)
            return {"events_polled": 0, "actions_taken": 0, "errors": 1, "skipped": 0, "imported": 0}

        for event in events:
            if event.id in seen:
                skipped += 1
                continue
            try:
                action = self._dispatch(event, imported_issues)
                if action:
                    actions_taken += 1
                    if action == "issue_imported":
                        imported += 1
                        imported_issues.add(event.issue_id)
                    log.info("reconciler: action=%s issue=%s event=%s", action, event.issue_id, event.id)
            except Exception as exc:
                errors += 1
                log.error("reconciler: dispatch error for event %s: %s", event.id, exc)
            finally:
                seen.add(event.id)

        # Safety-net: scan for untracked issues that didn't generate an activity event
        try:
            scan_imported = self._scan_untracked_issues(imported_issues)
            imported += scan_imported
        except Exception as exc:
            errors += 1
            log.error("reconciler: scan_untracked_issues failed: %s", exc)

        self._save_state(seen, imported_issues)

        # Emit a single audit event summarising the run.  Write directly to the
        # audit log (jsonl) because the sentinel task_id "__reconciler__" has no
        # matching row in the tasks table and would violate the DB FK constraint.
        try:
            self._service.audit.append(
                task_id="__reconciler__",
                event_type="reconciler_ran",
                payload={
                    "events_polled": len(events),
                    "actions_taken": actions_taken,
                    "errors": errors,
                    "skipped": skipped,
                    "imported": imported,
                },
                event_id=0,
            )
        except Exception:
            pass

        return {
            "events_polled": len(events),
            "actions_taken": actions_taken,
            "errors": errors,
            "skipped": skipped,
            "imported": imported,
        }

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, event: ActivityEvent, imported_issues: Optional[set[str]] = None) -> Optional[str]:
        """Route an activity event to the appropriate handler. Returns action name or None."""
        if imported_issues is None:
            imported_issues = set()
        entity_type = (event.entity_type or "").lower()
        # Routine run events are first-class and may not reference an issue.
        if entity_type == "routine_run":
            return self._handle_routine_run(event, imported_issues)

        # Routine / trigger events are metadata-level for now; capture in audit only.
        if entity_type in _ROUTINE_ENTITY_TYPES:
            self._emit_global(
                "reconciler_routine_event_seen",
                {
                    "paperclip_event_id": event.id,
                    "entity_type": event.entity_type,
                    "entity_id": event.entity_id,
                    "event_type": event.event_type,
                    "run_id": event.run_id,
                },
            )
            return None

        task = None
        if event.issue_id:
            task = self._service.db.get_task_by_paperclip_issue_id(event.issue_id)

        if task is None:
            # Unknown issue — check if it's a new issue we should import
            et = event.event_type.lower()
            if et in _ISSUE_CREATED_EVENT_TYPES and event.issue_id and event.issue_id not in imported_issues:
                return self._handle_new_issue(event, imported_issues)
            return None  # not a tracked issue and not an import trigger

        et = event.event_type.lower()

        if et in _COMMENT_EVENT_TYPES:
            return self._handle_comment(task, event)
        if et in _STATUS_EVENT_TYPES:
            return self._handle_status_change(task, event)

        return None

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_routine_run(self, event: ActivityEvent, imported_issues: set[str]) -> Optional[str]:
        """
        Reflect routine run activity into backend linkage.

        Routine runs are first-class in Paperclip and may complete without an
        issue (`coalesced` / `skipped`). For those cases we log a global audit
        event but intentionally do not create a backend task.
        """
        run_id = self._extract_run_id(event)
        if not run_id:
            return None
        routine_id = self._extract_routine_id(event)
        linked_issue_id = self._extract_linked_issue_id(event)
        run_status = self._extract_routine_run_status(event)

        task = self._service.db.get_task_by_paperclip_routine_run_id(run_id)
        if task is None and linked_issue_id:
            task = self._service.db.get_task_by_paperclip_issue_id(linked_issue_id)

        # If routine run has no issue-backed task, keep visibility in global audit.
        if task is None:
            if run_status in _ROUTINE_NON_TASK_TERMINAL_STATUSES:
                self._emit_global(
                    "reconciler_routine_run_non_task_terminal",
                    {
                        "paperclip_event_id": event.id,
                        "paperclip_routine_run_id": run_id,
                        "paperclip_routine_id": routine_id,
                        "paperclip_issue_id": linked_issue_id,
                        "status": run_status,
                    },
                )
                return "routine_run_non_task_terminal"

            # `issue_created` can race before issue activity arrives; attempt import
            # immediately when we have a linked issue id.
            if run_status == "issue_created" and linked_issue_id and linked_issue_id not in imported_issues:
                return self._import_issue_for_routine_run(
                    event,
                    imported_issues=imported_issues,
                    issue_id=linked_issue_id,
                    routine_id=routine_id,
                    run_id=run_id,
                )

            self._emit_global(
                "reconciler_routine_run_unlinked",
                {
                    "paperclip_event_id": event.id,
                    "paperclip_routine_run_id": run_id,
                    "paperclip_routine_id": routine_id,
                    "paperclip_issue_id": linked_issue_id,
                    "status": run_status,
                },
            )
            return None

        # Attach routine metadata to existing task.
        self._service.db.update_task(
            task.id,
            paperclip_routine_id=routine_id,
            paperclip_routine_run_id=run_id,
            paperclip_origin_kind="routine_execution",
            paperclip_issue_id=linked_issue_id or task.paperclip_issue_id,
        )
        self._emit_action(
            task.id,
            "routine_run_linked",
            event,
            {
                "paperclip_routine_id": routine_id,
                "paperclip_routine_run_id": run_id,
                "paperclip_issue_id": linked_issue_id,
                "status": run_status,
            },
        )
        return "routine_run_linked"

    def _import_issue_for_routine_run(
        self,
        event: ActivityEvent,
        *,
        imported_issues: set[str],
        issue_id: str,
        routine_id: Optional[str],
        run_id: str,
    ) -> Optional[str]:
        if self._cp is None:
            return None
        try:
            issue = self._cp._client.get_issue(issue_id)
        except Exception as exc:
            log.error("reconciler: could not fetch linked issue %s for routine run import: %s", issue_id, exc)
            return None
        try:
            result = self._service.import_paperclip_issue(
                issue_id=issue.id,
                title=issue.title,
                description="",
                project_id=issue.project_id,
                routine_id=routine_id,
                routine_run_id=run_id,
                origin_kind="routine_execution",
            )
            task = result["task"]
            imported_issues.add(issue.id)
            self._emit_action(
                task.id,
                "routine_run_issue_imported",
                event,
                {
                    "paperclip_issue_id": issue.id,
                    "paperclip_routine_id": routine_id,
                    "paperclip_routine_run_id": run_id,
                },
            )
            return "routine_run_issue_imported"
        except Exception as exc:
            log.error("reconciler: routine run import failed for issue %s: %s", issue_id, exc)
            return None

    @staticmethod
    def _extract_run_id(event: ActivityEvent) -> str:
        if event.run_id:
            return event.run_id
        if event.entity_id and (event.entity_type or "").lower() == "routine_run":
            return event.entity_id
        payload = event.payload or {}
        details = event.details or {}
        value = payload.get("runId") or payload.get("run_id") or details.get("runId") or details.get("run_id")
        return str(value) if value else ""

    @staticmethod
    def _extract_routine_id(event: ActivityEvent) -> Optional[str]:
        payload = event.payload or {}
        details = event.details or {}
        value = (
            payload.get("routineId")
            or payload.get("routine_id")
            or details.get("routineId")
            or details.get("routine_id")
        )
        if value:
            return str(value)
        if (event.entity_type or "").lower() == "routine" and event.entity_id:
            return event.entity_id
        return None

    @staticmethod
    def _extract_linked_issue_id(event: ActivityEvent) -> Optional[str]:
        payload = event.payload or {}
        details = event.details or {}
        value = (
            payload.get("linkedIssueId")
            or payload.get("linked_issue_id")
            or details.get("linkedIssueId")
            or details.get("linked_issue_id")
            or payload.get("issueId")
            or details.get("issueId")
            or event.issue_id
        )
        return str(value) if value else None

    @staticmethod
    def _extract_routine_run_status(event: ActivityEvent) -> str:
        payload = event.payload or {}
        details = event.details or {}
        value = (
            payload.get("status")
            or payload.get("newStatus")
            or payload.get("new_status")
            or details.get("status")
            or details.get("newStatus")
            or details.get("new_status")
        )
        return str(value).lower() if value else ""

    # ------------------------------------------------------------------
    # Paperclip-originated task import
    # ------------------------------------------------------------------

    def _handle_new_issue(self, event: ActivityEvent, imported_issues: set[str]) -> Optional[str]:
        """
        Import a manually-created Paperclip issue as a backend task.

        Fetches the full issue, infers domain from project_id, creates a task
        via service.import_paperclip_issue(), and marks the issue as imported.
        """
        if self._cp is None:
            return None
        try:
            issue = self._cp._client.get_issue(event.issue_id)
        except Exception as exc:
            log.error("reconciler: could not fetch issue %s for import: %s", event.issue_id, exc)
            return None

        try:
            result = self._service.import_paperclip_issue(
                issue_id=issue.id,
                title=issue.title,
                description="",  # get_issue may not return description; brief is in title
                project_id=issue.project_id,
                routine_id=self._extract_routine_id(event),
                routine_run_id=self._extract_run_id(event) or None,
                origin_kind="manual_issue",
            )
            task = result["task"]
            imported_issues.add(issue.id)
            log.info(
                "reconciler: imported Paperclip issue %s as task %s (domain=%s)",
                issue.id, task.id, task.domain,
            )
            try:
                self._service._append_event(
                    task_id=task.id,
                    event_type="reconciler_action_taken",
                    payload={
                        "action": "issue_imported",
                        "paperclip_event_id": event.id,
                        "issue_id": issue.id,
                        "issue_title": issue.title,
                    },
                )
            except Exception:
                pass
            return "issue_imported"
        except Exception as exc:
            log.error("reconciler: import_paperclip_issue failed for %s: %s", event.issue_id, exc)
            return None

    def _scan_untracked_issues(self, imported_issues: set[str]) -> int:
        """
        Safety-net scan: list all open Paperclip issues and import any that
        have no matching backend task and haven't been imported already.

        Returns the number of issues newly imported.
        """
        if self._cp is None:
            return 0

        try:
            issues = self._cp.list_all_issues(status="todo")
        except Exception as exc:
            log.error("reconciler: list_all_issues failed in scan: %s", exc)
            return 0

        imported = 0
        for issue in issues:
            if not issue.id:
                continue
            if issue.id in imported_issues:
                continue
            # Check if already tracked
            existing = self._service.db.get_task_by_paperclip_issue_id(issue.id)
            if existing is not None:
                imported_issues.add(issue.id)  # mark known so we stop re-checking
                continue
            # Not tracked — import it
            try:
                result = self._service.import_paperclip_issue(
                    issue_id=issue.id,
                    title=issue.title,
                    description="",
                    project_id=issue.project_id,
                    origin_kind="manual_issue",
                )
                task = result["task"]
                imported_issues.add(issue.id)
                imported += 1
                log.info(
                    "reconciler: scan imported issue %s as task %s",
                    issue.id, task.id,
                )
                try:
                    self._service._append_event(
                        task_id=task.id,
                        event_type="reconciler_action_taken",
                        payload={
                            "action": "issue_imported",
                            "source": "scan",
                            "issue_id": issue.id,
                            "issue_title": issue.title,
                        },
                    )
                except Exception:
                    pass
            except Exception as exc:
                log.error("reconciler: scan import failed for issue %s: %s", issue.id, exc)

        return imported

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

    def _emit_global(self, event_type: str, payload: dict[str, Any]) -> None:
        # Reconciler-global events are persisted in jsonl audit only.
        try:
            self._service.audit.append(
                task_id="__reconciler__",
                event_type=event_type,
                payload=payload,
                event_id=0,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # State persistence (seen event IDs + imported issue IDs)
    # ------------------------------------------------------------------

    def _load_state(self) -> tuple[set[str], set[str]]:
        """Return (seen_event_ids, imported_issue_ids)."""
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                return set(data.get("seen", [])), set(data.get("imported_issues", []))
        except Exception as exc:
            log.warning("reconciler: could not load state file: %s", exc)
        return set(), set()

    def _save_state(self, seen: set[str], imported_issues: set[str]) -> None:
        try:
            trimmed_seen = list(seen)
            if len(trimmed_seen) > _MAX_SEEN:
                trimmed_seen = trimmed_seen[-_MAX_SEEN:]
            # imported_issues is a set of Paperclip issue IDs — keep all (they're UUIDs, bounded)
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps({"seen": trimmed_seen, "imported_issues": list(imported_issues)}, indent=None),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("reconciler: could not save state file: %s", exc)

    # Back-compat helpers retained for legacy tests and tooling.
    def _load_seen(self) -> set[str]:
        seen, _ = self._load_state()
        return seen

    def _save_seen(self, seen: set[str]) -> None:
        _, imported_issues = self._load_state()
        self._save_state(seen, imported_issues)


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
