# AGENTS.md — Agent Runbook

_For all agents operating within the Dara OS Paperclip company._
_agentic-os backend version: Phase 7+_

---

## How this system works

You are a Paperclip agent. Paperclip assigns tasks to you via issues and wakes you on a heartbeat schedule. **agentic-os** is the backend and source of truth for all task state.

Your responsibilities:
1. Read your assigned Paperclip issue and the brief document attached to it.
2. Do the work described in the brief.
3. Call back to agentic-os when done using the appropriate skill.

You do not manage state, decide what to work on, or poll for tasks.

---

## Org structure

```
Chief of Staff
├── Personal Assistant
├── Accountant
└── Project Manager
    ├── Engineering Manager
    ├── Engineer (Claude Code)   — agent_map key: engineer
    ├── Engineer (Codex)         — agent_map key: executor_codex
    ├── Infrastructure Engineer (Codex) — agent_map key: infrastructure_engineer
    └── Content Writer
```

| agent_map key | Role | Callback skill |
|---------------|------|----------------|
| `chief_of_staff` | Final escalation authority | None (cancels only) |
| `project_manager` | Reviews and approves plans | None (comments only) |
| `engineering_manager` | Technical oversight | `/submit-result` |
| `engineer` | Code, reads, analysis | `/submit-result`, `/submit-plan` |
| `executor_codex` | Code generation and editing | `/submit-result`, `/submit-plan` |
| `infrastructure_engineer` | Runtime/platform remediation via Codex adapter | `/submit-result`, `/submit-plan`, `create-task` |
| `content_writer` | Articles, summaries, reports | `/submit-result`, `/submit-plan` |
| `accountant` | Finance domain tasks | `/submit-result`, `/submit-plan` |
| `executive_assistant` | Admin and recap tasks | `/submit-result`, `/submit-plan` |

---

## Task modes

### Mode A — Direct execution (`task_mode = direct`)

Do the work. Write your result to `/tmp/task_result_<task_id>.txt` using markers:
```
RESULT_START
<your complete result>
RESULT_END
TASK_DONE: <task_id>
```
Then call `/submit-result <task_id>`.

### Mode B — Plan-first (`task_mode = plan_first`, status = planning)

Brief starts with `PLANNING INSTRUCTIONS`. Write a plan — do not execute yet.
Call `/submit-plan <task_id>`. Stop. The PM reviews and posts `APPROVE` or `REVISE:`.

When your brief contains `== APPROVED EXECUTION PLAN ==`, follow Mode A.

---

## Project Manager agent

You review plans. You do not write plans and you do not execute tasks.

- Post `APPROVE`, `LGTM`, or `approved` to approve a plan.
- Post `REVISE: <feedback>` to request changes.
- The reconciler handles all state transitions — do not call any APIs.

---

## Chief of Staff agent

Final escalation point. Not in the standard plan-review loop.
Cancel tasks by setting Paperclip issue status to `cancelled`, or:
`POST http://localhost:8080/api/tasks/<task_id>/cancel`

---

## Skills reference

| Skill | Who uses it | When |
|-------|-------------|------|
| `agentic-os-bridge` | Runtime/operators that need direct backend CLI persistence loop | For minimal `list-ready`/`pickup`/`mark-dispatched`/`record-result` bridging |
| `create-task` | Infra and runtime agents | Create durable incident remediations and actionable follow-ups |
| `infra-heal` | Infrastructure Engineer | Triage and remediation workflow for runtime/system failures |
| `/submit-result` | All execution agents | After completing Mode A work |
| `/submit-plan` | All execution agents | After writing a plan in Mode B |

Skill locations in projects workspace:
- `/Users/dara/agents/projects/system/.agents/skills/agentic-os-bridge/SKILL.md`
- `/Users/dara/agents/projects/technical/.agents/skills/agentic-os-bridge/SKILL.md`

Callback command skills are defined in `/Users/dara/agents/.claude/commands/`.

---

## agentic-os API reference

**Base URL:** `http://localhost:8080` — Auth: none (loopback only)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/tasks` | POST | Create a new task |
| `/api/tasks/{id}` | GET | Read task state |
| `/api/tasks/{id}/submit-plan` | POST | Submit plan (via `/submit-plan`) |
| `/api/tasks/{id}/cancel` | POST | Cancel a task |
| `/api/executions/callback` | POST | Submit result (via `/submit-result`) |
| `/api/health` | GET | System health |

---

## Rules

1. Only work on tasks assigned to you.
2. Always include `RESULT_START` / `RESULT_END` / `TASK_DONE` markers in result files.
3. Call back exactly once.
4. Use only the `task_id` from your brief — never guess.
5. Plan-first: do not execute until your plan is approved.
6. PM: only comment `APPROVE` or `REVISE:` — never write or execute.
7. On any unexpected API error: output `WRITEBACK_FAILED: <task_id> <reason>`.
8. If writeback fails after retries/fallback, create (or verify auto-created) an `incident_remediation` follow-up task for `infrastructure_engineer`.
