# Codex Brief — Build a Phase 1 Notion Adapter for agentic-os

## Mission
Implement a **thin Notion adapter** for `agentic-os` that fits the revised Phase 1 architecture.

This is **not** a full orchestration engine and **not** a broad Notion platform abstraction.
It should be a narrow integration layer that allows OpenClaw + `agentic-os` to:
- create Notion tasks/tickets
- list/query relevant Notion tasks
- update Notion task status
- write short agent notes / planning summaries back to Notion
- mirror enough identifiers/status to keep backend task state linked to Notion items

## Architectural constraints
This work must follow `revised-phase1-architecture.md` and the Phase 1 plans already written in the workspace.

Relevant architecture intent:
- OpenClaw remains the conversational control plane
- `agentic-os` remains the durable operational backend
- Notion is the human-facing planning/task surface
- no separate orchestration engine
- no multi-agent runtime
- no giant workflow layer

The adapter should be **thin, explicit, and boring**.

## Read these first
Before implementation, read:
- `/Users/dara/.openclaw/workspace/revised-phase1-architecture.md`
- `/Users/dara/.openclaw/workspace/PHASE1_OPENCLAW_CLASSIFICATION_ROUTING_PLAN.md`
- `/Users/dara/.openclaw/workspace/PHASE1_NOTION_PLAN.md`

Then inspect the current `agentic-os` codebase and fit the adapter into the existing service/storage model.

## Product goal
Enable this Phase 1 loop:
1. Dara can ask OpenClaw to create a task/ticket in Notion
2. the backend can store the resulting Notion external ref
3. the system can read/query Notion tasks by status or updated time
4. cron/heartbeat-friendly polling can detect new Notion items
5. OpenClaw can generate a lightweight plan artifact for a new Notion task
6. backend can update Notion status and append an agent note back to the task
7. all important state remains durable and auditable in `agentic-os`

## Scope

### Implement now
Build a thin Notion adapter with these capabilities:

#### Read/query
- query tasks/items from a configured Notion database
- support filtering by:
  - status
  - updated since timestamp (if practical)
  - page size / limit
- get a single item detail by page id (if needed for clean implementation)

#### Write
- create a task/item in a configured Notion database
- update status on an existing Notion page
- update arbitrary selected properties needed for task tracking
- append a short note / plan summary to an existing page

#### Backend integration
- store Notion page id / external ref on backend task records
- provide a clean service path for:
  - create Notion task
  - sync/query Notion tasks
  - push status/note updates back to Notion
- emit audit events for adapter calls/results/failures

#### Config
Add config support for:
- Notion API token
- default workspace/profile concept only if truly needed
- Notion tasks database id
- property-name mapping for fields like:
  - title
  - status
  - type
  - area/domain
  - backend task id
  - operation key
  - last agent update

Keep config small and explicit.

### Optional if easy
- a small CLI surface for manual testing
- helper methods for status mapping between backend and Notion

## Do NOT build
- no Notion orchestration engine
- no autonomous planner subsystem
- no broad “sync everything” framework
- no complicated schema migration manager
- no generic multi-provider adapter framework
- no approval UI inside Notion
- no background worker system

## Design guidance

### 1. Keep the adapter narrow
Suggested adapter responsibilities:
- `create_task(...)`
- `query_tasks(...)`
- `update_task_status(...)`
- `append_note(...)`
- maybe `get_task(...)`

Avoid giant methods like:
- `execute_workflow(...)`
- `sync_everything(...)`

### 2. Prefer explicit property mapping
Notion databases vary. Do not hardcode too much hidden behavior.
Use config for the property names you need.

### 3. Handle failures explicitly
Adapter must clearly surface and record:
- bad auth/token
- database/page not found
- invalid property mappings
- rate limits
- malformed responses
- timeouts

Write audit events and fail clearly. Never silently swallow adapter failures.

### 4. Preserve Phase 1 split
OpenClaw should still do:
- request interpretation
- classification
- summaries
- approval conversations

The adapter should only perform narrow Notion interactions.

## Suggested implementation shape
Adjust names if needed, but keep it simple.

Potential files:
- `src/agentic_os/adapters/notion.py` or similar
- service integration changes in `src/agentic_os/service.py`
- config wiring in `src/agentic_os/config.py`
- CLI additions in `src/agentic_os/cli.py` if needed
- tests if you can add them cheaply without bloating scope

If the repo currently uses a flatter layout, stay consistent with that style.

## Recommended config shape
Use a small config block, for example:

```json
{
  "notion": {
    "apiTokenEnv": "NOTION_API_KEY",
    "databaseId": "<notion database id>",
    "properties": {
      "title": "Title",
      "status": "Status",
      "type": "Type",
      "area": "Area",
      "backendTaskId": "OpenClaw Task ID",
      "operationKey": "Operation Key",
      "lastAgentUpdate": "Last Agent Update"
    },
    "statusMap": {
      "new": "Inbox",
      "in_progress": "In Progress",
      "awaiting_input": "Waiting",
      "awaiting_approval": "Review",
      "completed": "Done",
      "failed": "Blocked",
      "cancelled": "Cancelled"
    }
  }
}
```

You may adjust exact names/schema if a better fit emerges, but keep it explicit and minimal.

## Required behavior

### Create task in Notion
Given backend task metadata, create a Notion row/page with:
- title
- status
- type
- area/domain
- backend task id if available
- operation key if available

Return the created Notion page id and persist it as external ref.

### Query tasks from Notion
Support listing tasks by status and/or recent changes so cron/heartbeat can use it later.
Return normalized data, not raw unreadable Notion blobs.

### Update task status in Notion
Given backend status change, map it to Notion status and update the row.

### Append agent note / plan
Allow backend/OpenClaw to append a short note or plan summary to the Notion page body.
This is meant for lightweight planning artifacts, not giant document generation.

## Audit expectations
Adapter operations should emit meaningful audit events, for example:
- `adapter_called`
- `adapter_result`
- `adapter_failed`
- or a narrower naming pattern if the codebase already has conventions

At minimum, Notion interactions must be inspectable later.

## Verification expectations
Do a lightweight but real verification pass.
If a real Notion workspace is not configured in the environment, still:
- ensure code imports cleanly
- ensure routes/CLI/service methods wire up correctly
- validate payload building logic
- document what config/user secrets are still needed for live verification

## Deliverables
Produce:
- working thin Notion adapter integrated into the repo
- config support/documentation
- service integration points
- any minimal CLI/testing helpers you add
- updated `README.md` documenting setup and usage

## Final instruction
Implement the smallest coherent Notion adapter that enables Phase 1 task creation/query/status update/note append workflows while preserving the revised architecture: OpenClaw thinks, `agentic-os` records, Notion acts as the planning surface.
