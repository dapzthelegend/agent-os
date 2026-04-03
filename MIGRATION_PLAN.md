# Migration Plan: Notion → Paperclip Control Plane

_Produced: 2026-03-24. Planning document only — no code changes._

## Goal

Replace Notion with Paperclip as the operator-facing control plane, while keeping `agentic-os` as the canonical source of truth for task state, routing, scheduling, delivery, and policy.

## Core Rules

1. Backend remains the source of truth.
2. Paperclip is a projection and review surface only.
3. OpenClaw delivery stays backend-owned.
4. New org structure is used for all routing and assignment.
5. Incremental DB migrations are dropped. Recreate schema from scratch.

## Target Architecture

- **OpenClaw**: intake + final delivery surface
- **agentic-os backend**: orchestration, policy, routing, scheduling, execution state
- **Paperclip**: issues, comments, documents, operator review
- **No Notion in live orchestration**

## Paperclip Org Model

### Company
- `Dara OS`

### Goal
- `Task Execution`

### Projects
- `personal`
- `technical`
- `finance`
- `system`

### Agents
- `Chief of Staff`
- `Project Manager`
- `Engineering Manager`
- `Engineer`
- `Engineer (Codex)`
- `Content Writer`
- `Accountant`
- `Executive Assistant`

### Reporting
- `Chief of Staff` is top-level
- `Project Manager` reports to `Chief of Staff`
- `Engineering Manager`, `Engineer`, `Engineer (Codex)`, `Content Writer` report to `Project Manager`
- `Accountant`, `Executive Assistant` report to `Chief of Staff`

## Phase Overview

| Phase | Name | Outcome |
|---|---|---|
| 0 | Foundation | Config, fresh schema, Paperclip client, control plane stub |
| 1 | Projection | Backend tasks create/update Paperclip issues |
| 2 | Plan Gate | `plan_first` flow with Paperclip plan docs and review |
| 3 | Execution Writeback | Results/comments/documents/artifacts written to Paperclip |
| 4 | Reconciler | Poll operator actions from Paperclip activity/comments |
| 5 | API / UI | Approve/reject/retry/health endpoints |
| 6 | Notion Cutover | Disable Notion task orchestration paths |

---

## Phase 0 — Foundation

### Manual Bootstrap  --- DONE

One-time Paperclip setup:
- create company
- create goal
- create 4 projects
- create 8 agents
- record stable IDs in config

### Config (`agentic_os.config.json`)

Add: --- DONE

```json
{
  "paperclip": {
    "base_url": "http://localhost:3100/api",
    "auth_mode": "trusted",
    "company_id": "",
    "goal_id": "",
    "project_map": {
      "personal": "",
      "technical": "",
      "finance": "",
      "system": ""
    },
    "agent_map": {
      "chief_of_staff": "",
      "project_manager": "",
      "engineering_manager": "",
      "engineer": "",
      "executor_codex": "",
      "content_writer": "",
      "accountant": "",
      "executive_assistant": ""
    },
    "reconcile_poll_interval_seconds": 120,
    "reconcile_activity_lookback_seconds": 300
  }
}
```

### Config loading

Update `config.py`:
- add `PaperclipConfig`
- validate `company_id`, `goal_id`, `project_map`, `agent_map`
- raise `ConfigurationError` if Paperclip is enabled but incomplete

### Database reset

Do **not** add `ALTER TABLE` migrations.

Instead:
- delete the old task DB
- recreate schema from scratch
- define the new task schema as canonical
- remove backward-compat migration logic

Add a reset/bootstrap script:
- `scripts/reset_db.py`

Update `storage.py`:
- initialize schema from canonical DDL
- no incremental migrations
- fail fast on incompatible old schema instead of patching it

### Tasks schema (fresh)

Required fields:
- `id`
- `title`
- `description`
- `domain`
- `status`
- `task_mode`
- `paperclip_issue_id`
- `paperclip_assignee_agent_id`
- `paperclip_project_id`
- `paperclip_goal_id`
- `plan_version`
- `approved_plan_revision_id`
- `delivery_target`
- `delivery_thread_id`
- `result_summary`
- `artifact_path`
- timestamps (`created_at`, `updated_at`, etc.)

### Status set

```text
new
planning
awaiting_plan_review
approved_for_execution
executing
awaiting_approval
completed
failed
stalled
cancelled
```

No legacy `in_progress` compatibility state.

### `paperclip_client.py`

Create a typed HTTP wrapper with:
- trusted/API-key auth handling
- retry on transient failures only
- typed methods for issues/comments/documents/attachments/activity
- structured error logging

Phase 0 scope:
- no checkout/release logic yet

### `task_control_plane.py`

Create control-plane abstraction:
- create/update Paperclip issue projections
- add comments
- write plan docs
- upload artifacts
- poll activity

Failures must be logged and swallowed so Paperclip outages do not crash backend flows.

### Default routing map

Inside control plane:
- planning / coordination → `project_manager`
- escalation / review → `chief_of_staff`
- Claude-side execution → `engineer`
- Codex execution → `executor_codex`
- writing tasks → `content_writer`
- finance tasks → `accountant`
- admin/scheduling → `executive_assistant`

### Phase 0 deliverable

- Paperclip config validated
- DB recreated from scratch
- `paperclip_client.py` added
- `task_control_plane.py` added
- no live behavior change yet

---

## Phase 1 — Projection Layer

### Goal

Every backend task creates a matching Paperclip issue and later status updates propagate.

### Wiring

After backend task creation:
- create Paperclip issue
- store returned `paperclip_issue_id`
- if Paperclip fails, backend task still succeeds

### Initial issue mapping

- title ← user request / short task title
- description ← short human summary only
- project ← `project_map[task.domain]`
- goal ← `goal_id`
- assignee ← chosen by backend routing
- Paperclip status ← backend status mapping

### Backend → Paperclip status map

| Backend | Paperclip |
|---|---|
| `new` | `todo` |
| `planning` | `in_progress` |
| `awaiting_plan_review` | `in_review` |
| `approved_for_execution` | `todo` |
| `executing` | `in_progress` |
| `awaiting_approval` | `blocked` |
| `completed` | `done` |
| `failed` | `blocked` |
| `stalled` | `blocked` |
| `cancelled` | `cancelled` |

### Writeback

On terminal states:
- post completion/failure comment to Paperclip
- clear assignee where appropriate

### Phase 1 deliverable

- new tasks project into Paperclip
- Paperclip mirrors backend status changes
- Paperclip unavailability never blocks task creation

---


## Phase 2 — Plan Gate

### Goal

Introduce `plan_first` for complex tasks.

### Modes

* `direct`
* `plan_first`

### Default classification

Use `plan_first` for:

* higher-risk work
* complex technical work
* writing/proposal work when review is needed

Everything else defaults to `direct`.

### Plan flow

`new -> planning -> awaiting_plan_review -> approved_for_execution -> executing -> completed|failed|cancelled`

### Planner / reviewer mapping with new org

* plan generation → `engineering_manager`, `engineer`, `executor_codex`, or `content_writer`
* plan review / approval → `project_manager`

### Implementation

* backend selects the appropriate executor-side specialist to create the plan
* backend writes plan to Paperclip `documents/plan`
* issue moves to `in_review`
* `project_manager` approves/rejects through Paperclip comments/status
* approval stores `approved_plan_revision_id`
* execution starts only after approved revision is locked

### Phase 2 deliverable

* `plan_first` tasks cannot execute without PM approval
* plans are created by the eventual executor path
* approved plan revision is canonical

---


## Phase 3 — Execution Writeback

### Goal

Replace Notion writeback with Paperclip writeback.

### Rules

- short result → Paperclip comment
- long result → `documents/result` + short comment
- artifact → upload attachment + comment
- failure/block → failure comment + status update

### Assignment

Execution agent chosen by backend policy:
- normal engineering / Claude path → `engineer`
- repo/code-heavy Codex path → `executor_codex`
- writing deliverable → `content_writer`
- finance deliverable → `accountant`
- admin task → `executive_assistant`

### Phase 3 deliverable

- execution results are fully written to Paperclip
- all Notion result writeback paths removed from live flow

---

## Phase 4 — Reconciler

### Goal

Detect operator actions in Paperclip and reflect them into backend state.

### Scope

Poll activity/comments only. No full sync engine.

### Reconciled actions

- plan approval comments
- revision request comments
- status → cancelled
- status → blocked
- selected operator comments for unblock / review

### Comment parsing

Approval signals:
- `APPROVE`
- `LGTM`
- `approved`

Revision signals:
- `REVISE:`
- `REVISION:`
- `REQUEST_REVISION:`

Ambiguous comments are logged only.

### Scheduling

Run as lightweight periodic poll:
- preferred: cron/heartbeat job
- fallback: background loop

### Phase 4 deliverable

- operator approvals/rejections/cancellations are reflected in backend
- reconciler is idempotent and failure-tolerant

---

## Phase 5 — API / UI

### Goal

Expose minimal Paperclip-related operator controls.

### Routes

Add thin endpoints for:
- approve plan
- reject plan
- retry task
- set task mode
- Paperclip health

### Dashboard

Add a minimal Paperclip health page showing:
- Paperclip reachability
- last reconcile status/time
- count of tasks with / without `paperclip_issue_id`

### Phase 5 deliverable

- operator-facing backend routes exist
- basic Paperclip status page exists

---

## Phase 6 — Notion Cutover

### Goal

Remove Notion from all live orchestration paths.

### Changes

Disable:
- `notion-monitor-poll`
- `notion-sync`

Enable:
- `paperclip-reconcile`

Deprecate live use of:
- `notion_monitor.py`
- `notion_sync.py`
- `notion_result_writer.py`

Keep if still needed outside orchestration:
- `notion.py` for routine/context reads only

### Post-cutover intake

Tasks enter through:
- OpenClaw conversation
- CLI / API
- backend internal routines

No automated Notion inbox/task polling remains.

### Phase 6 deliverable

- zero Notion task orchestration in live path
- Paperclip is the only operator-facing task surface

---

## File Change Summary

### New
- `src/agentic_os/paperclip_client.py`
- `src/agentic_os/task_control_plane.py`
- `src/agentic_os/paperclip_reconciler.py`
- `scripts/reset_db.py`

### Update
- `src/agentic_os/config.py`
- `src/agentic_os/storage.py`
- `src/agentic_os/service.py`
- `src/agentic_os/dispatcher.py`
- `src/agentic_os/execution_receiver.py`
- `src/agentic_os/api_routes.py`
- `src/agentic_os/web_routes.py`
- `agentic_os.config.json`
- `cron/jobs.json`

### Deprecate from live orchestration
- `src/agentic_os/notion_monitor.py`
- `src/agentic_os/notion_sync.py`
- `src/agentic_os/notion_result_writer.py`

---

## Verification

### Direct task
- create task
- backend creates Paperclip issue
- execute
- result/comment shows in Paperclip
- final delivery still goes to OpenClaw

### Plan-first task
- create task
- planner writes Paperclip plan doc
- operator approves in Paperclip
- executor runs against approved revision
- final result written back to Paperclip and OpenClaw

### Manual operator intervention
- operator cancels or requests revision in Paperclip
- backend reflects change within reconcile window

### Cutover
- Notion jobs disabled
- end-to-end task flow succeeds without Notion calls
