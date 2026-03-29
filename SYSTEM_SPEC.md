# agentic-os — System Specification

_Current as of 2026-03-26. Phases 0–7 complete. Paperclip is the live operator surface._

---

## Overview

agentic-os is the durable backend for an OpenClaw-powered agentic OS. It owns:

- **Task state** — full lifecycle from intake through planning, approval, execution, and completion
- **Policy decisions** — which tasks require plans, drafts, approvals, or can auto-execute
- **Artifact versioning** — versioned file storage for plans, drafts, and outputs
- **Audit trail** — append-only JSONL + SQLite event log (42 event types)
- **Integration adapters** — Paperclip (live), Gmail (3 accounts), Google Calendar (3 accounts), Discord

**Core principle:** agentic-os is the source of truth. Paperclip is a projection and review surface. Notion has been removed from all live orchestration paths.

---

## Architecture

```
                        ┌─────────────────────────────────────────┐
                        │          agentic-os backend              │
                        │                                         │
                        │  Intake → intake_classifier             │
                        │        → dispatcher (builds brief doc)  │
                        │        → plan_gate (plan_first)         │
                        │        → service (state machine)        │
                        │        → decision_engine                │
                        │        → approval workflow              │
                        │        → execution_receiver             │
                        │        → notifier                       │
                        │                                         │
                        │  Paperclip sync                         │
                        │    task_control_plane  (writeback)      │
                        │    paperclip_reconciler (poll, 2 min)   │
                        │                                         │
                        │  Daily routine: calendar_poller         │
                        │               + gmail_poller            │
                        │               + daily_routine           │
                        │               + gmail_sender            │
                        │                                         │
                        │  Dashboard: FastAPI (localhost:8080)    │
                        │  Storage: SQLite + JSONL audit log      │
                        └──────┬─────────────────▲───────────────┘
                               │                 │
             issues / briefs / │                 │ POST /api/executions/callback
             comments / docs   │                 │ POST /api/tasks/{id}/submit-plan
                               ▼                 │ (agents call back when done)
                 ┌─────────────────────────────────────┐
                 │              Paperclip               │
                 │  http://localhost:3100/api           │
                 │  auth_mode: trusted                  │
                 │                                     │
                 │  Company: Dara OS                   │
                 │  Goal: Task Execution               │
                 │  Projects: personal / technical /   │
                 │            finance / system         │
                 │  Agents: Chief of Staff, PM, EM,    │
                 │          Engineer, Codex, Content   │
                 │          Writer, Accountant, EA     │
                 │                                     │
                 │  ★ Owns: heartbeat scheduling,      │
                 │    agent wakeup, session lifecycle, │
                 │    atomic task checkout, budgets    │
                 └─────────────────────────────────────┘
```

**Data flow in plain terms:**

1. Task is created in agentic-os → `task_control_plane` opens a Paperclip issue, attaches the brief document, assigns the correct agent.
2. Paperclip's heartbeat timer detects the assignment and triggers the agent (Claude Code, Codex, or another executor) on its next cycle.
3. The agent reads its Paperclip issue (brief, context), executes the work, then calls `POST /api/executions/callback` (or `POST /api/tasks/{id}/submit-plan` for plan-first) on agentic-os when done.
4. `paperclip_reconciler` polls Paperclip activity every 2 minutes to reflect any operator comments or status changes back into agentic-os state.

**Reverse flow (Paperclip → agentic-os):**

Tasks can also be created manually in Paperclip. On every reconciler cycle:

5. Activity events with type `issue_created` for an untracked issue trigger `import_paperclip_issue()`.
6. A safety-net scan also checks all open `todo` issues and imports any with no matching backend task.
7. Import: domain inferred from `project_id`, classification defaults to `execute` / `medium`, `action_source = paperclip_manual`.
8. The existing Paperclip issue is *adopted* (not replaced): status + assignee updated, callback brief written as a document.
9. Lifecycle proceeds as normal from `new` status.

agentic-os never initiates agent sessions or owns scheduling. It only creates/updates Paperclip issues and waits for agents to call back.

---

## Task Flow

### Direct task (`task_mode = direct`)

```
new → in_progress → completed / failed / stalled
                 ↑
              retry
```

### Plan-first task (`task_mode = plan_first`)

```
new → planning ──────────────────────────────────────────────────────────────────────────────────┐
       (execution agent writes plan,                                                             │
        submits via POST /submit-plan)                                                           │
          │                                                                                      │
          ▼                                                                                      │
      awaiting_plan_review ──REVISE (PM comment)──────────────────────────────── back to planning
       (assigned to PM for review)
          │
      APPROVE (PM Paperclip comment, web UI, or API)
          │
          ▼
      approved_for_execution
       (reassigned to original execution agent)
          │
          ▼
      executing → completed / failed
```

- The **execution agent** (engineer, codex, content writer, etc.) writes the plan — not the PM.
- The **Project Manager** reviews and approves or requests revisions.
- On approval, the **same execution agent** is reassigned to execute.

### Approval-required task

```
new → awaiting_approval → approved → in_progress → completed / failed
                       └─ denied
```

---

## Components

### Core State Machine (`service.py`, `storage.py`)

Central orchestrator. Methods include:

| Method | Action |
|--------|--------|
| `create_request()` | Intake, classify, route, project to Paperclip |
| `approve_plan()` | Transition `awaiting_plan_review` → `approved_for_execution` |
| `reject_plan()` | Transition `awaiting_plan_review` → `planning` with feedback |
| `cancel_task()` | Direct cancellation, no Paperclip writeback (used by reconciler) |
| `set_task_mode()` | Override `plan_first` / `direct` mode |
| `complete_task()` | Store artifact, write result to Paperclip, notify |
| `fail_task()` | Mark failed, write failure to Paperclip |
| `retry_task()` | Re-enter `in_progress` |
| `approve() / deny() / cancel()` | Approval workflow decisions |

### Plan Gate (`plan_gate.py`)

Classifies tasks as `plan_first` or `direct` based on domain, intent_type, and risk_level.

`plan_first` by default for: `high` risk, `technical` domain with `execute` intent, `content` tasks with review flags.

### Paperclip Integration

#### `paperclip_client.py`

Low-level typed HTTP client. Trusted auth. Retry on transient failures. Methods:
- `create_issue`, `update_issue`, `get_issue`
- `add_comment`, `list_comments`
- `write_document`, `get_document`
- `upload_attachment`
- `list_activity`, `list_recent_activity`

#### `task_control_plane.py`

High-level control plane abstraction used by `service.py`. Methods:
- `create_issue` — project to Paperclip on task creation
- `update_issue_status` — mirror backend status changes
- `add_comment` — post result/failure summaries
- `write_plan_doc` — upload plan to `documents/plan`
- `upload_artifact` — attach file outputs to issue
- `post_result_comment` / `post_failure_comment` — structured writeback
- `poll_company_activity` — used by reconciler
- `write_result` — full result writeback (short → comment; long → doc + comment; artifact → attachment)

All Paperclip failures are caught and logged as `paperclip_sync_failed` audit events. Paperclip outages never block backend task operations.

#### `paperclip_reconciler.py`

Polls Paperclip company activity every 2 minutes. Reflects operator actions into backend state.

| Paperclip action | Backend effect |
|-----------------|---------------|
| Comment: `APPROVE` / `LGTM` / `approved` | `approve_plan()` if task in `awaiting_plan_review` |
| Comment: `REVISE:` / `REVISION:` / `REQUEST_REVISION:` | `reject_plan()` if task in `awaiting_plan_review` |
| Status → `cancelled` | `cancel_task()` if task not terminal |
| Ambiguous comment | Logged only (`reconciler_comment_ignored`) |
| `issue_created` event for untracked issue | `import_paperclip_issue()` — adopts issue as new backend task |
| Open `todo` issue with no backend task (scan) | `import_paperclip_issue()` — safety-net catch for missed events |

- **Idempotent:** seen event IDs + imported issue IDs persisted in `data/paperclip_reconciler_state.json` (rolling 2000-entry window for events; all imported issue IDs retained)
- **Failure-tolerant:** per-event try/except; poll failure returns error count, never crashes
- **No Paperclip writeback** on reconciler-sourced cancellations (avoids feedback loops)

### Execution Pipeline

| Module | Role |
|--------|------|
| `execution_receiver.py` | Receives agent results via `POST /api/executions/callback`. Deduplicates by `operation_key` or `dispatch_session_key`. Stores artifact. Calls `complete_task()`. |
| `task_control_plane.write_result()` | Writeback to Paperclip: short → comment; long → doc; artifact → attachment |
| `notifier.py` | Discord/Gmail completion notifications |

Agents (triggered by Paperclip's heartbeat) call `POST /api/executions/callback` when their work is done. agentic-os never initiates or manages the agent sessions. Notion writeback is fully removed from this path (Phase 3/6 complete).

### Runtime Skill Symlink Hydration

Runtime-visible skills are materialized into each workspace as symlinks (not copies):

- Target path: `projects/<project>/.agents/skills/<skill_name>`
- Source spec: `/Users/dara/agents/runtime-skills-spec.json`
- Hydrator: `/Users/dara/agents/bin/hydrate-runtime-skills`

Hydration rules:

1. Effective project skillset = `global_defaults + shared_integrations + project_defaults[project]` (deduped, order preserved).
2. Each skill is resolved from `source_roots` in order (first match wins).
3. Hydrator creates/updates symlinks in workspace `.agents/skills`.
4. Existing local skill directories with `SKILL.md` are preserved.
5. Existing symlinks that already point to a valid skill of the same name are preserved.

Automation:

- `bin/agents-stack` runs `bin/hydrate-runtime-skills` at stack startup before runtime services launch.
- This ensures Paperclip/Codex runs starting from `projects/<project>` see the project-appropriate skills without per-workspace duplication.

### Intake Pipeline

| Module | Role |
|--------|------|
| `intake_classifier.py` | Maps domain / intent_type / risk_level → route / agent key |
| `dispatcher.py` | Builds structured task brief documents (posted to Paperclip issue) |
| `decision_engine.py` | Policy evaluation → `read_ok` / `draft_required` / `approval_required` |

The brief document is written to the Paperclip issue at task creation time. The assigned agent reads it when Paperclip's heartbeat wakes it. agentic-os has no further involvement in session initiation.

**Current task intake sources:**
- OpenClaw conversation (manual operator input via CLI or service call)
- Backend internal routines (daily routine follow-up tasks)
- `POST /api/tasks` HTTP endpoint
- **Paperclip manual creation** — operator creates an issue directly in Paperclip; reconciler detects and imports it (`action_source = paperclip_manual`)
- *(Notion polling: disabled as of Phase 6)*

### Deprecated Modules (Notion — retained for rollback)

| Module | Status |
|--------|--------|
| `notion_monitor.py` | DEPRECATED — `notion-monitor-poll` cron disabled |
| `notion_result_writer.py` | DEPRECATED — writeback removed from all live paths |
| `notion_sync.py` | DEPRECATED — no cron caller; CLI still available for one-off use |
| `notion.py` | Active for daily routine context reads only |

### Approval Workflow

| Module | Role |
|--------|------|
| `approval_notifier.py` | Builds approval request payload, sends via router |
| `notification_router.py` | Discord DM (primary) → Gmail (fallback) |
| `email_approval_parser.py` | Parses APPROVE/DENY from email replies |

### Daily Routine (`daily_routine.py`)

Triggered at 08:30 London time. Gathers:
- Today's calendar (from `calendar_poller`)
- Personal inbox summaries (from `gmail_poller`)
- Agent inbox (from `gmail_poller`)

Produces:
- Structured recap with recommended actions
- Follow-up backend tasks
- HTML recap email to `franchieinc@gmail.com`
- Durable `daily_routine_recap` task record with full artifacts

Note: Notion input was part of daily routine previously. As of Phase 6, daily routine may use `notion.py` for context reads only; automated Notion task sync is disabled.

### Web Dashboard (`web.py`, `web_routes.py`, `api_routes.py`)

Local operator console at `http://127.0.0.1:8080`. Loopback-only.

**Pages:**
- `/` — Overview: task counts by status, domain breakdown, system health
- `/tasks` — List with filters (status, domain, target, action_source)
- `/tasks/{id}` — Detail: request, artifacts, approvals, audit timeline
- `/approvals` — Queue with pending/approved/denied/cancelled filters
- `/approvals/{id}` — Detail + approve / deny / cancel actions
- `/executions/{operation_key}` — Execution detail
- `/audit` — Append-only event timeline (newest first)
- `/recaps` — Recap views (today, approvals, drafts, failures, overdue, external-actions)
- `/stalled` — Stalled tasks with retry actions
- `/health` — System health (DB, audit log, artifacts, cron jobs)
- `/paperclip` — Paperclip connectivity, last reconcile time, task coverage counts

**JSON API:**
```
GET  /api/health
GET  /api/overview
GET  /api/tasks
GET  /api/tasks/{id}
GET  /api/approvals
GET  /api/approvals/{id}
GET  /api/executions/{key}
GET  /api/audit
GET  /api/recap/{today|approvals|drafts|failures|overdue|external-actions|in-progress}
GET  /api/paperclip/health

POST /api/approvals/{id}/approve
POST /api/approvals/{id}/deny
POST /api/approvals/{id}/cancel
POST /api/tasks/{id}/retry
POST /api/tasks/{id}/approve-plan
POST /api/tasks/{id}/reject-plan
POST /api/tasks/{id}/cancel
POST /api/tasks/{id}/set-mode
POST /api/tasks
POST /api/tasks/{id}/submit-plan
POST /api/artifacts/{id}/revise
POST /api/executions/callback
```

### Resilience (`recovery.py`, `health.py`, `backup.py`)

- **Stall detection:** tasks in `in_progress` past per-domain thresholds (content=6h, personal=4h, finance=4h, default=2h) flagged as stalled with Discord alert
- **Retry:** `retry_task()` re-enters task into `in_progress`
- **Health checks:** DB reachability, artifact directory, config validation, cron job status, Paperclip reachability
- **Backup:** SQLite snapshot to `~/.openclaw/backups/agentic-os/{date}/`

---

## Data Model

### Task Schema

```
id, created_at, updated_at
domain, intent_type, risk_level
status, approval_state
task_mode                        -- 'direct' | 'plan_first'
paperclip_issue_id               -- Paperclip issue ID (nullable)
plan_version                     -- INTEGER, incremented per plan submission
approved_plan_revision_id        -- set on approve_plan()
user_request, result_summary
artifact_ref, external_ref, operation_key
target, request_metadata_json
policy_decision, action_source
claimed_at, claimed_by
dispatch_session_key, dispatch_attempts, retry_count
```

### Status Set

```
new
planning
awaiting_plan_review
approved_for_execution
executing
in_progress
awaiting_approval
awaiting_input
completed
failed
stalled
cancelled
```

### Audit Events (42 types)

Core: `task_created`, `task_classified`, `policy_evaluated`, `task_completed`, `task_failed`, `task_cancelled`, `task_retry_reset`, `task_stalled`, `task_stall_cleared`

Execution: `task_picked_up`, `task_dispatched`, `task_requeued`, `spawn_failed`, `action_executed`, `action_execution_requested`, `action_execution_recorded`, `action_execution_rejected`, `execution_callback_received`

Approval: `approval_requested`, `approval_granted`, `approval_denied`, `approval_cancelled`, `operation_rejected`

Adapter: `adapter_called`, `adapter_result`, `adapter_failed`, `tool_called`, `tool_result`

Artifacts / drafts: `draft_created`, `draft_generated`, `artifact_updated`

Daily routine: `daily_routine_recap_created`, `daily_routine_email_prepared`, `daily_routine_followup_created`, `daily_routine_followups_created`, `summary_recorded`

Notion (legacy): `notion_sync_imported`, `notion_update_failed`

Paperclip projection: `paperclip_issue_created`, `paperclip_issue_imported`, `paperclip_projection_failed`, `paperclip_sync_failed`

Plan gate: `task_mode_set`, `plan_submitted`, `plan_awaiting_review`, `plan_approved`, `plan_rejected`

Reconciler: `reconciler_ran`, `reconciler_action_taken`, `reconciler_comment_ignored`

---

## Scheduled Jobs

> **Scheduling boundary:** All background jobs run inside the agentic-os FastAPI process via `scheduler.py` (`InternalScheduler`). No external cron is used. `cron/jobs.json` is deprecated. Paperclip's heartbeat timer is solely responsible for waking agents when a task is assigned to them.

| Job | Schedule | Owner | Description |
|-----|----------|-------|-------------|
| `paperclip-reconcile` | every 2 min | `scheduler.py` | Poll Paperclip activity, reflect operator actions + import manually-created issues |
| `stall-check` | every 1h | `scheduler.py` | Flag tasks stalled past domain threshold, send Discord alert |
| `approval-reminder` | every 1h | `scheduler.py` | Remind on approvals pending >1h |
| `health-check` | every 1h | `scheduler.py` | Log health snapshot, Discord alert on error |
| `workspace-backup` | daily 02:00 UTC | `scheduler.py` | SQLite + artifacts backup, 7-day retention |
| `daily-routine` | daily 08:30 London | `scheduler.py` | Calendar + inbox recap + email |
| *(agent heartbeat)* | per Paperclip config | **Paperclip** | Paperclip wakes assigned agents on each heartbeat cycle — not an agentic-os concern |

---

## Paperclip Org Structure

| Entity | Details |
|--------|---------|
| Company | `Dara OS` |
| Goal | `Task Execution` |
| Projects | `personal`, `technical`, `finance`, `system` |
| Agents | `Chief of Staff`, `Project Manager`, `Engineering Manager`, `Engineer`, `Engineer (Codex)`, `Content Writer`, `Accountant`, `Executive Assistant` |

### Backend → Paperclip Status Map

| Backend status | Paperclip status | Paperclip assignee |
|----------------|-----------------|-------------------|
| `new` | `todo` | execution agent |
| `planning` | `in_progress` | execution agent (writing plan) |
| `awaiting_plan_review` | `in_review` | `project_manager` (reviewing plan) |
| `approved_for_execution` | `todo` | execution agent (ready to execute) |
| `executing` / `in_progress` | `in_progress` | execution agent |
| `awaiting_approval` | `blocked` | — |
| `completed` | `done` | — |
| `failed` | `blocked` | — |
| `stalled` | `blocked` | — |
| `cancelled` | `cancelled` | — |

---

## Configuration

### `agentic_os.config.json`

```json
{
  "paperclip": {
    "base_url": "http://localhost:3100/api",
    "auth_mode": "trusted",
    "company_id": "<uuid>",
    "goal_id": "<uuid>",
    "project_map": {
      "personal": "<uuid>",
      "technical": "<uuid>",
      "finance": "<uuid>",
      "system": "<uuid>"
    },
    "agent_map": {
      "chief_of_staff": "<uuid>",
      "project_manager": "<uuid>",
      "engineering_manager": "<uuid>",
      "engineer": "<uuid>",
      "executor_codex": "<uuid>",
      "content_writer": "<uuid>",
      "accountant": "<uuid>",
      "executive_assistant": "<uuid>"
    },
    "reconcile_poll_interval_seconds": 120,
    "reconcile_activity_lookback_seconds": 300
  },
  "stallThresholds": {
    "default": 2.0,
    "content": 6.0,
    "personal": 4.0,
    "finance": 4.0
  }
}
```

### OAuth Credentials

| File | Account | Scopes |
|------|---------|--------|
| `gog.json` | `franchieinc@gmail.com` | gmail.readonly, gmail.send, calendar (write) |
| `gog_dapz.json` | `dapzthelegend@gmail.com` | gmail.readonly, calendar.readonly |
| `gog_sola.json` | `solaaremuoluwadara@gmail.com` | gmail.readonly, calendar.readonly |

Re-authorize: `python3 scripts/reauth_google.py --all`

---

## Running the System

### Prerequisites

```bash
cd /Users/dara/agents/agentic-os
pip install -e .
export PYTHONPATH=src
```

### Initialize / reset DB

```bash
python3 scripts/reset_db.py     # drops and recreates schema
python3 -m agentic_os.cli init  # init with existing schema
```

### Start Dashboard

```bash
uvicorn agentic_os.web:app --reload --host 127.0.0.1 --port 8080
```

### Run Reconciler (one-shot)

```bash
PYTHONPATH=src python3 -m agentic_os.paperclip_reconciler
```

### Health Check

```bash
python3 -m agentic_os.cli health
curl http://localhost:8080/api/health
curl http://localhost:8080/api/paperclip/health
```

### Backup

```bash
python3 -m agentic_os.backup
```

---

## Capability Status

| Capability | Status | Notes |
|------------|--------|-------|
| Task intake & classification | ✅ | 17 routing rules; `POST /api/tasks` HTTP endpoint |
| Plan gate (plan_first flow) | ✅ | Phases 2 + 4 |
| Policy decision engine | ✅ | read_ok / draft_required / approval_required |
| Approval workflow | ✅ | Email + Discord, reply-based |
| Execution result capture | ✅ | operation_key dedup; session_key fallback; terminal-state guard |
| Live execution loop (plan_first end-to-end) | ✅ | Phase 7: submit-plan API, planning brief, codex routing, callback hardening |
| Paperclip issue projection | ✅ | All new tasks mirrored to Paperclip |
| Paperclip status writeback | ✅ | All state transitions mirrored |
| Paperclip result/plan writeback | ✅ | Comment / doc / attachment by result size |
| Paperclip reconciler (poll) | ✅ | Every 2 min, idempotent |
| Plan approve / reject via Paperclip | ✅ | Comment-based + API + web UI |
| Task cancel via Paperclip | ✅ | Reconciler-driven, no feedback loop |
| Task import from Paperclip (manual creation) | ✅ | Reconciler: issue_created events + safety-net scan |
| Paperclip health dashboard | ✅ | /paperclip page + /api/paperclip/health |
| Notion task monitoring | ❌ | Disabled (Phase 6) |
| Notion result writeback | ❌ | Removed (Phase 3 + 6) |
| Gmail polling (3 accounts) | ✅ | Agent + 2 personal |
| Email approval reply parsing | ✅ | APPROVE/DENY parsing |
| Task completion notifications | ✅ | Discord primary, Gmail fallback |
| Calendar polling (3 accounts) | ✅ | Merged, sorted by start time |
| Calendar write (agent inbox) | ✅ | Create, update, delete, block, remind |
| Daily recap generation | ✅ | Calendar + inboxes + recommended actions |
| Daily recap email delivery | ✅ | HTML/MIME, cron at 08:30 London |
| Web dashboard | ✅ | 11 pages + JSON API |
| Audit trail | ✅ | JSONL + SQLite, 42 event types |
| Stall detection & recovery | ✅ | Per-domain thresholds |
| Approval reminders | ✅ | Cron every 1h |
| SQLite backup | ✅ | Local to ~/.openclaw/backups/ |
| Personal inbox OAuth | ⚠️ | gog_dapz.json / gog_sola.json may need re-auth |
| Discord bot commands | ❌ | Notifications only |
| Off-site backup | ❌ | Local only |

---

## Next Phases

### Phase 7 — Live Execution Loop Hardening ✅ Complete

All gaps resolved:

| Gap | Status |
|-----|--------|
| `submit-plan` API endpoint | ✅ `POST /api/tasks/{id}/submit-plan` added |
| Planning brief for plan_first tasks | ✅ PM brief replaced with PLANNING INSTRUCTIONS block |
| Codex routing (`executor_codex`) | ✅ `resolve_executor_key` reads classifier agent from `request_metadata_json` |
| `notion_page_id` removed from live paths | ✅ Replaced with `paperclip_issue_id` throughout |
| `execution-callback` hardening | ✅ session_key fallback; terminal guard; no 5xx; task-not-found → 400 |
| End-to-end smoke test | ✅ `tests/test_e2e_plan_first_flow.py` (10 tests) |

---

### What is still required for a fully running system

The agentic-os backend is complete. Paperclip owns scheduling and agent execution — that is also in place. What remains are two categories of gap:

**A. agentic-os API gaps**

| Missing piece | Description |
|---------------|-------------|
| **Task intake endpoint** (`POST /api/tasks`) | ✅ Implemented. Accepts `user_request`, `domain`, `intent_type`, `risk_level`, optional `operation_key` / `target` / `request_metadata`. Returns `task_id`, `status`, `task_mode`, `paperclip_issue_id`. |
| **Discord interactive approvals** | Approval notifications go out via Discord DM but replies are not parsed. Operators must approve via the web UI (`http://localhost:8080/approvals`) or a Paperclip comment. |
| **Off-site backup** | Backups are local only (`~/.openclaw/backups/`). No S3/rclone sync configured. |
| **Personal inbox OAuth** | `gog_dapz.json` / `gog_sola.json` may need re-auth for full 3-account Gmail coverage. |

**B. Paperclip agent configuration (not agentic-os code)**

See **`AGENTS.md`** for the full agent runbook — output format, callback protocol, plan submission protocol, and API reference. These are configurations inside Paperclip, not changes to this codebase:

| Agent config gap | Description |
|-----------------|-------------|
| **Agent session templates pass the brief** | Paperclip must be configured to surface the issue's brief document to the agent when the heartbeat triggers it. The brief already contains all callback/submit-plan instructions. |
| **Codex agent template** | The Paperclip agent record for `executor_codex` must be linked to a live Codex session template. The backend correctly routes to this key — the Paperclip side just needs to be wired. |

---

### Phase 8 — Observability & Ops

| Gap | Action |
|-----|--------|
| Discord interactive approvals | Discord bot with DM reply parsing (approve/deny). Until then: use web UI or Paperclip comments. |
| Personal inbox OAuth re-auth | Run `scripts/reauth_google.py --account dapz,sola` if `gog_dapz.json` / `gog_sola.json` expire. |
| Off-site backup | S3 / rclone sync after local backup |
| Paperclip reconciler metrics | Expose reconciler run history as API endpoint |
| Alert on `paperclip_sync_failed` | Discord alert when Paperclip writeback fails repeatedly |
| Stall threshold tuning | Review per-domain thresholds against observed task durations |

### Phase 9 — Audit & Search

| Gap | Action |
|-----|--------|
| Audit log search | Full-text or indexed query on audit JSONL |
| Execution history view | Per-agent execution summary (how many tasks, avg time, failure rate) |

---

## File Layout

```
src/agentic_os/
  models.py                Data classes
  storage.py               SQLite layer
  service.py               State machine & business logic
  config.py                Config loading
  audit.py                 Append-only event log (42 event types)
  artifacts.py             File-based versioning
  health.py                Health checks (DB, artifacts, cron, Paperclip)

  intake_classifier.py     Domain/intent/risk → route/agent key
  dispatcher.py            Builds brief documents posted to Paperclip issues
  plan_gate.py             plan_first vs direct classification
  decision_engine.py       Policy evaluation
  execution_receiver.py    Agent result ingestion (Paperclip writeback, no Notion)

  paperclip_client.py      Typed HTTP client for Paperclip API
  task_control_plane.py    High-level Paperclip abstraction used by service.py
  paperclip_reconciler.py  Poll + reflect operator actions from Paperclip

  notion.py                Notion client (daily routine context reads only)
  notion_monitor.py        DEPRECATED — Notion inbox polling
  notion_sync.py           DEPRECATED — Bidirectional Notion sync
  notion_result_writer.py  DEPRECATED — Notion result writeback

  gmail_sender.py          Send emails (MIME/HTML)
  gmail_poller.py          3-account inbox polling
  gmail_task_creator.py    Auto-create tasks from personal inbox

  daily_routine.py         Recap builder
  calendar_poller.py       3-account calendar polling
  calendar_writer.py       Calendar event management

  approval_notifier.py     Approval request notifications
  notifier.py              Task completion notifications
  notification_router.py   Discord → Gmail fallback chain
  email_approval_parser.py Parse APPROVE/DENY replies

  web.py                   FastAPI entrypoint
  web_routes.py            HTML page routes (11 pages)
  web_support.py           Template helpers
  api_routes.py            JSON API routes (20 endpoints)
  templates/               Jinja2 templates
    base.html, overview.html, tasks.html, task_detail.html
    approvals.html, approval_detail.html, execution_detail.html
    audit.html, health.html, stalled.html, recaps.html
    paperclip.html         Paperclip health dashboard

  cli.py                   CLI (200+ commands)
  backup.py                SQLite backup
  recovery.py              Task retry/recovery
  validation.py            Input validation
  adapters.py              Custom adapter seam

data/
  agentic_os.sqlite3                Primary database
  agentic_os.audit.jsonl            Append-only event log
  paperclip_reconciler_state.json   Seen event IDs (idempotency)

artifacts/
  task_{id}/                        Per-task versioned artifacts
    art_{id}.v{n}.{ext}

scripts/
  reset_db.py                       Drop + recreate schema from scratch
  openclaw_daily_routine_bridge.py
  openclaw_daily_routine_runner.py
  openclaw_notion_sync_bridge.py
  openclaw_calendar_bridge.py
  reauth_google.py
  healthcheck.py

cron/                               Workspace-local cron job definitions
  jobs.json

AGENTS.md                           Agent runbook (output format, callback, plan submission)
tests/                              202 tests, all passing
  test_e2e_plan_first_flow.py       Phase 7 end-to-end smoke test (10 tests)
```
