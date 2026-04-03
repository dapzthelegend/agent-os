# Backend Brief: Paperclip Routine Task Support

Date: 2026-03-29

## Context

Paperclip routines are in beta and introduce a distinct execution model from one-off issues/tasks.
In Paperclip, a **routine** can produce **routine runs**; some runs create a linked issue, others end as `coalesced`/`skipped` with **no issue created**.

Today, `agentic-os` is issue-centric and assumes Paperclip activity can be reconciled via `issueId`.
This causes ambiguity when routine-originated runs are present because a run may not map to a task row.

## What Paperclip Supports (Source-of-truth)

1. First-class routine resources and APIs:
- `GET/POST /api/companies/{companyId}/routines`
- `GET/PATCH /api/routines/{routineId}`
- trigger APIs (`/triggers`, `/rotate-secret`)
- run APIs (`POST /api/routines/{routineId}/run`, `GET /api/routines/{routineId}/runs`)

2. Routine run states include non-task outcomes:
- `received`, `coalesced`, `skipped`, `issue_created`, `completed`, `failed`

3. Routine run <-> issue linkage is optional:
- `routine_runs.linked_issue_id` is nullable.
- execution issues carry `origin_kind = 'routine_execution'`, `origin_id = routineId`, `origin_run_id = runId`.

4. Activity supports routine entity types in code paths:
- `routine`, `routine_trigger`, `routine_run` are logged entity types.

5. Run identity is separate from issue identity:
- mutating heartbeat calls use `X-Paperclip-Run-Id`.

## What agentic-os Currently Supports

1. Single integration key:
- backend task row stores `paperclip_issue_id` only.

2. Reconciler logic is issue-only:
- unknown activity events are only imported when interpreted as issue-created events.
- no routine/routine_run/routine_trigger handling.

3. Activity parser assumes issue linkage:
- `ActivityEvent.issue_id` is derived from `issueId`; events without issue IDs collapse to empty/unknown issue mapping.

4. No routine API client surface:
- `PaperclipClient` has issues/comments/docs/attachments/activity only.

## Root Cause of Breakage

The backend currently conflates:
- "task entity" (agentic-os internal task)
- "Paperclip issue" (execution work item)
- "Paperclip routine run" (scheduler/webhook/API execution attempt)

With routines beta, a run is not guaranteed to be an issue. Therefore, issue-only reconciliation cannot represent routine lifecycle accurately.

## Required Backend Support (Minimum)

1. Add first-class routine projections in agentic-os
- New columns (or table) for external references:
  - `paperclip_routine_id`
  - `paperclip_routine_run_id`
  - `paperclip_origin_kind` (`manual_issue`, `routine_execution`, `routine_coalesced`, etc.)
- Keep `paperclip_issue_id` nullable and independent.

2. Expand Paperclip client
- Add typed methods:
  - `list_routines(company_id)`
  - `get_routine(routine_id)`
  - `list_routine_runs(routine_id, limit)`
  - optional `run_routine(routine_id, payload)` for operator tooling
- Extend activity parsing to capture:
  - `entityType`
  - `entityId`
  - `runId`
  - `details` (raw)

3. Reconciler v2 event routing
- Route by `entityType` first, not only `eventType`/`issueId`.
- Add handlers:
  - `entityType=issue` -> existing flow
  - `entityType=routine_run` -> update/create backend execution record, attach linked issue when present
  - `entityType=routine` / `routine_trigger` -> optional metadata sync + audit-only fallback
- Preserve idempotency using `(activity_event_id)` and `(routine_run_id)` uniqueness guards.

4. Canonical state model mapping
- Internal execution state should map both run-only and issue-backed outcomes:
  - `received` -> `queued`
  - `coalesced` / `skipped` -> terminal non-task completion
  - `issue_created` -> task-linked execution pending/completing via issue lifecycle
  - `completed` / `failed` -> terminal
- Do not auto-create backend tasks for coalesced/skipped runs.

5. Correlation and observability
- Store and emit correlation IDs in audit logs and web views:
  - `paperclip_run_id` (heartbeat)
  - `paperclip_routine_run_id`
  - `paperclip_issue_id`
- Add dashboards/filters by external entity type to avoid "unknown task" events.

## Recommended Rollout Plan

1. Schema + model extension (non-breaking, nullable fields).
2. Client + parser extension with feature flag `paperclip_routines_enabled`.
3. Reconciler dual-path support (issue + routine_run).
4. Backfill: for existing routine execution issues, infer routine linkage from issue metadata/comments where possible.
5. Enable flag in staging; verify no duplicate imports and no false task creation on `coalesced`/`skipped` runs.
6. Enable in prod after 7-day observation window.

## Acceptance Criteria

1. A routine run with `status=coalesced` creates no backend task but is visible in audit with preserved IDs.
2. A routine run with `status=issue_created` links to exactly one backend task once the issue appears.
3. Issue status transitions still reconcile correctly for both manual and routine-originated issues.
4. Reconciler remains idempotent across restarts and repeated activity polling.
5. No regression to existing plan approval/comment workflows.

## Key Sources

- Official docs currently published:
  - https://docs.paperclip.ing/api/overview
  - https://docs.paperclip.ing/api/issues
  - https://docs.paperclip.ing/api/activity

- Paperclip source/docs (beta routines details):
  - https://github.com/paperclipai/paperclip/blob/main/docs/api/routines.md
  - https://github.com/paperclipai/paperclip/blob/main/packages/db/src/schema/routines.ts
  - https://github.com/paperclipai/paperclip/blob/main/server/src/services/routines.ts
  - https://github.com/paperclipai/paperclip/blob/main/server/src/routes/routines.ts
  - https://github.com/paperclipai/paperclip/blob/main/packages/shared/src/constants.ts

- agentic-os current integration points:
  - src/agentic_os/paperclip_client.py
  - src/agentic_os/task_control_plane.py
  - src/agentic_os/paperclip_reconciler.py
  - src/agentic_os/models.py
