# Week 5 Codex Implementation Brief — Local Operator Dashboard for agentic-os

## Mission
Implement a **local-only dashboard/frontend** for `agentic-os` that exposes the Week 4 backend through a usable operator UI.

This is **not** a chat UI and **not** a public SaaS app. It is a local operator console for inspecting and acting on durable agent state.

## Product goal
Make the Week 4 backend operationally usable without living in the CLI.

By the end of this implementation, a local operator should be able to:
- inspect tasks
- filter tasks
- inspect full task details
- inspect approvals
- approve / deny / cancel pending approvals
- inspect artifacts and revisions
- revise artifacts
- inspect executions
- inspect audit history
- inspect recap / summary views

## Current backend state (already exists)
Repo: `agentic-os`

Current backend already provides:
- SQLite durable storage
- JSONL audit log
- artifact versioning
- approvals
- idempotent execution records
- recap commands
- OpenClaw-backed request recording

Important Week 4 concepts already in place:
- `tasks`
- `artifacts`
- `approvals`
- `executions`
- `audit_events`
- `action_source` provenance:
  - `openclaw_tool`
  - `openclaw_skill`
  - `custom_adapter`
  - `manual`

Current CLI surface already exists and should be treated as a reference for supported operations:
- `task list|show|trace|complete|fail|execute`
- `approval list|show|approve|deny|cancel`
- `execution show`
- `audit tail`
- `recap today|approvals|drafts|failures|external-actions`
- `artifact revise`
- `openclaw read|draft|execution`
- `adapter execute` (future seam only)

## Implementation principles
1. **Reuse existing service logic**. Do not duplicate policy or state-transition logic in the web layer.
2. **Local-first only**. Bind to `127.0.0.1` by default.
3. **Keep architecture boring**.
4. **Do not add unnecessary infrastructure** like provider SDKs, auth systems, websocket complexity, or multi-tenant abstractions.
5. **Operator clarity beats polish**.

## Recommended stack
Use:
- **FastAPI** for local HTTP serving and JSON endpoints
- **Jinja2 templates** for server-rendered HTML
- **HTMX** for light interactivity where useful
- lightweight CSS only (simple custom CSS or Pico.css)

Avoid adding a heavy SPA unless absolutely necessary.

## Scope for Week 5

### 1. Dashboard overview page
A homepage that shows:
- counts by status
- pending approvals count
- open drafts count
- recent failures count
- recent external actions count
- recent tasks list

### 2. Tasks list page
Display tasks with:
- id
- status
- domain
- intent
- target
- risk
- action source
- created/updated timestamps

Support filters for:
- status
- domain
- target
- action source

### 3. Task detail page
Show:
- task metadata
- request text
- policy decision
- approval state
- operation key
- result summary
- linked artifacts
- linked approvals
- linked execution record
- audit event timeline

### 4. Approvals queue page
Show pending approvals clearly.
For each approval, support:
- view details
- approve
- deny
- cancel
- optional decision note

### 5. Artifact review + revision
Support:
- viewing artifact content
- viewing revision history
- revising artifact content for a task

For Week 5, plain text / JSON rendering is enough.

### 6. Execution detail page
Display execution info by operation key:
- operation key
- linked task
- linked approval
- execution status
- result summary
- timestamps

### 7. Audit viewer page
Display a readable audit timeline.
Prefer newest-first.
Make payloads readable.
Highlight important event types:
- `policy_evaluated`
- `draft_generated`
- `summary_recorded`
- `action_execution_requested`
- `action_execution_recorded`
- rejection events

### 8. Recap views
Expose recap outputs for:
- today
- approvals
- drafts
- failures
- external-actions

## Suggested file/module layout
You may adjust names if needed, but keep things simple and coherent.

Suggested additions:
- `src/agentic_os/web.py` — FastAPI app entrypoint
- `src/agentic_os/web_routes.py` — HTML routes
- `src/agentic_os/api_routes.py` — JSON API routes
- `src/agentic_os/templates/` — Jinja templates
- `src/agentic_os/static/` — CSS assets

If fewer files are cleaner, that is fine.

## Required routes/pages

### HTML pages
- `/` → dashboard overview
- `/tasks` → task list
- `/tasks/{task_id}` → task detail
- `/approvals` → approvals queue
- `/approvals/{approval_id}` → approval detail (optional if queue view is enough)
- `/executions/{operation_key}` → execution detail
- `/audit` → audit timeline
- `/recaps` or dedicated recap pages

### JSON endpoints
Implement a minimal, useful JSON API:
- `GET /api/overview`
- `GET /api/tasks`
- `GET /api/tasks/{task_id}`
- `GET /api/approvals`
- `GET /api/approvals/{approval_id}` if useful
- `GET /api/executions/{operation_key}`
- `GET /api/audit`
- `GET /api/recap/today`
- `GET /api/recap/approvals`
- `GET /api/recap/drafts`
- `GET /api/recap/failures`
- `GET /api/recap/external-actions`

### Mutation endpoints
- `POST /api/approvals/{approval_id}/approve`
- `POST /api/approvals/{approval_id}/deny`
- `POST /api/approvals/{approval_id}/cancel`
- `POST /api/artifacts/{task_id}/revise`

If useful, HTML form posts may wrap these directly.

## UX requirements
- keep it fast
- keep it readable
- keep state transitions obvious
- make pending approvals highly visible
- make risk visible
- make action source visible
- make audit trails readable

Suggested visual semantics:
- `completed` → green
- `awaiting_approval` → amber
- `awaiting_input` → blue
- `failed` → red
- `cancelled` → gray
- risk badges for low/medium/high
- action-source badges

## Non-goals
Do **not** add any of the following in this implementation unless absolutely required:
- remote auth
- multi-user tenancy
- websocket/live sync
- background workers
- provider-specific integrations
- generalized plugin architecture
- public deployment hardening
- replacing the CLI

## Security / serving constraints
- bind to `127.0.0.1` by default
- local-only serving
- no public exposure assumptions
- no secrets in templates or logs

## Acceptance criteria
Implementation is complete when:
1. a local operator can run the web app on loopback
2. the dashboard homepage shows useful summary state
3. tasks can be listed and filtered
4. task detail is inspectable
5. approvals can be reviewed and acted on
6. artifacts can be reviewed and revised
7. executions can be inspected
8. audit events can be inspected in a readable way
9. recap views are accessible from the UI
10. all state changes go through the existing backend/service layer rather than duplicated logic

## Suggested run command
A sensible local dev run command should work, for example:

```bash
PYTHONPATH=src uvicorn agentic_os.web:app --reload --host 127.0.0.1 --port 8080
```

If additional dependencies are required, update project config accordingly.

## Documentation updates required
Update `README.md` to include:
- what the dashboard is
- how to run it locally
- what routes/pages exist
- local-only serving note

## Deliverables
Produce:
- working web app in-repo
- minimal styling
- template files
- route handlers
- any necessary helper functions
- updated docs

## Implementation preference
Bias toward the smallest coherent implementation that satisfies the acceptance criteria.
Do not gold-plate.

## Final instruction
Implement the dashboard directly in this repo using the existing backend/service layer as the source of truth. Keep it local, simple, and operator-focused.
