# agentic-os

Week 4 local-first backend for an OpenClaw-first agentic OS, now with a thin Phase 1 Notion adapter.

The default architecture is intentionally boring:

- OpenClaw tools and skills perform reads, drafting, and execution when those capabilities already exist.
- `agentic-os` stores durable task state, policy decisions, artifact revisions, approvals, execution records, and audit history.
- Future custom adapters are an explicit escape hatch, not the default integration path.
- Phase 1 includes one narrow exception: an explicit Notion task adapter for human-facing planning/task tracking.

Week 5 adds a local-only operator dashboard over the same backend/service layer. It is intentionally small: FastAPI serves server-rendered Jinja views and JSON endpoints against the existing durable state.

Phase 1 daily routine support is now included as a thin backend layer:

- OpenClaw remains responsible for Gmail and calendar reads plus cron/email-send control.
- `agentic-os` accepts normalized daily-routine inputs, generates the recap, stores it durably, and creates conservative follow-up tasks.
- Where appropriate, `agentic-os` can mirror those follow-ups into Notion using the existing thin Notion adapter.

## Layout

- `src/agentic_os/` - backend package and CLI
- `src/agentic_os/web.py` - FastAPI dashboard entrypoint
- `src/agentic_os/templates/` - server-rendered dashboard templates
- `src/agentic_os/static/` - dashboard stylesheet
- `data/` - SQLite database and JSONL audit log
- `artifacts/` - versioned artifact files
- `policy_rules.json` - local policy branching rules

## Week 4 model

Tasks now also record `action_source` so an operator can trace whether work came from:

- `openclaw_tool`
- `openclaw_skill`
- `custom_adapter`
- `manual`

Week 2 primitives remain the core model:

- `tasks` - durable work items and policy outcomes
- `artifacts` - stored outputs and revisions
- `approvals` - pending and decided approval records
- `executions` - one execution per `operation_key`
- `audit_events` plus `data/audit_log.jsonl` - append-only trace

Week 4 keeps the unified backend and adds:

- clearer operator inspection via `task show`, `approval show`, `execution show`, and `audit tail`
- filterable task listing via `--status`, `--domain`, `--target`, and `--action-source`
- simple recap commands over durable state via `recap today|approvals|drafts|failures|external-actions`
- consistent rejection tracing via `operation_rejected` audit events for invalid task/approval operations

OpenClaw-backed audit detail includes:

- `tool_called`
- `tool_result`
- `draft_generated`
- `summary_recorded`
- `action_execution_requested`
- `action_execution_recorded`

## Philosophy

Use the OpenClaw path by default:

- If OpenClaw already has a calendar, inbox, or other capability, let OpenClaw do the action.
- Record the durable state here.
- Keep custom adapters small and explicit.
- The Notion-specific adapter in [`src/agentic_os/notion.py`](/Users/dara/.openclaw/workspace/agentic-os/src/agentic_os/notion.py) is intentionally narrow: create/query/get/update-status/append-note only.

The generic adapter seam in [`src/agentic_os/adapters.py`](/Users/dara/.openclaw/workspace/agentic-os/src/agentic_os/adapters.py) still raises `CustomAdapterNotImplementedError`. Only Notion is implemented.

## Notion config

Create [`agentic_os.config.json`](/Users/dara/.openclaw/workspace/agentic-os/agentic_os.config.json) in the repo root, using [`agentic_os.config.example.json`](/Users/dara/.openclaw/workspace/agentic-os/agentic_os.config.example.json) as the template, and set the token env var it references:

```json
{
  "notion": {
    "apiTokenEnv": "NOTION_API_KEY",
    "databaseId": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "dataSourceId": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "properties": {
      "title": "Title",
      "status": "Status",
      "type": "Type",
      "area": "Area",
      "backendTaskId": "OpenClaw Task ID",
      "operationKey": "Operation Key",
      "lastAgentUpdate": "Last Agent Update"
    },
    "propertyKinds": {
      "type": "select",
      "area": "select"
    },
    "statusMap": {
      "new": "Inbox",
      "in_progress": "In Progress",
      "awaiting_input": "Waiting",
      "awaiting_approval": "Review",
      "approved": "In Progress",
      "executed": "In Progress",
      "completed": "Done",
      "failed": "Blocked",
      "cancelled": "Cancelled"
    }
  }
}
```

`dataSourceId` is optional and preferred for modern Notion databases. When `dataSourceId` is used, the adapter automatically sends Notion's current data-source API version for those calls. `databaseId` remains supported for legacy database-query semantics. For backward compatibility, if only `databaseId` is set and that value is actually a Notion data source id, the adapter will retry the query/create call against the modern data-source endpoint automatically.

`propertyKinds` is optional. Use it when your Notion schema defines `Type` and/or `Area` (or a mapped property such as `Domain`) as `multi_select` instead of `select`.

Then export the token before running any Notion command:

```bash
export NOTION_API_KEY="secret_..."
```

Optional: set `AGENTIC_OS_CONFIG_PATH` if you want the config JSON to live somewhere other than the repo root.

The configured Notion database or data source should expose these property types:

- `Title`: title
- `Status`: status
- `Type`: select or multi_select
- `Area`: select or multi_select
- `OpenClaw Task ID`: rich text
- `Operation Key`: rich text
- `Last Agent Update`: rich text

## Quick start

Run from the repo root with `PYTHONPATH=src`.

```bash
PYTHONPATH=src python3 -m agentic_os.cli init
PYTHONPATH=src python3 -m agentic_os.cli --help
```

## Thin Notion adapter

The Notion adapter follows the existing service/storage/audit path:

- each query or create call records a backend task
- Notion page ids are stored in `tasks.external_ref`
- adapter calls emit `adapter_called`, `adapter_result`, or `adapter_failed` audit events
- status and note pushes operate against the existing backend task + linked `external_ref`

Safe Notion capture/update targets are explicitly allowed in [`policy_rules.json`](/Users/dara/.openclaw/workspace/agentic-os/policy_rules.json). Other external writes still fall back to approval-required behavior.

Examples:

```bash
PYTHONPATH=src python3 -m agentic_os.cli notion create-task \
  --domain technical \
  --risk low \
  --request "Create a Notion task for the dashboard bug" \
  --title "Fix dashboard bug" \
  --status Inbox \
  --task-type bug \
  --area technical \
  --operation-key notion-task-dashboard-bug

PYTHONPATH=src python3 -m agentic_os.cli notion query-tasks \
  --status Inbox \
  --updated-since 2026-03-20T00:00:00Z \
  --limit 10

PYTHONPATH=src python3 -m agentic_os.cli notion sync-tasks \
  --status Inbox \
  --status Review \
  --updated-since 2026-03-20T00:00:00Z \
  --limit 25

PYTHONPATH=src python3 -m agentic_os.cli notion get-task <page_id>

PYTHONPATH=src python3 -m agentic_os.cli notion update-status task_000001 \
  --backend-status completed \
  --note "Finished and reflected back to Notion."

PYTHONPATH=src python3 -m agentic_os.cli notion append-note task_000001 \
  --note "Proposed plan: reproduce, patch, verify, then close."
```

## Dashboard

The dashboard is a local operator console for inspecting tasks, approvals, artifacts, executions, audit history, and recap views without living in the CLI.

Install dependencies, then run it on loopback only:

```bash
PYTHONPATH=src python3 -m pip install -e .
PYTHONPATH=src uvicorn agentic_os.web:app --reload --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080`.

Important: this dashboard is intended for local-only use. The default run command binds to `127.0.0.1` and nothing in this repo assumes public exposure.

Primary pages:

- `/` overview dashboard
- `/tasks` filterable task list
- `/tasks/{task_id}` task detail with artifacts, approvals, execution links, audit timeline, and artifact revision form
- `/approvals` approvals queue and decision views
- `/approvals/{approval_id}` approval detail
- `/executions/{operation_key}` execution detail
- `/audit` audit timeline
- `/recaps` service-backed recap views

JSON endpoints:

- `/api/overview`
- `/api/tasks`
- `/api/tasks/{task_id}`
- `/api/approvals`
- `/api/approvals/{approval_id}`
- `/api/executions/{operation_key}`
- `/api/audit`
- `/api/recap/today`
- `/api/recap/approvals`
- `/api/recap/drafts`
- `/api/recap/failures`
- `/api/recap/external-actions`

## Daily routine flow

The daily routine path is intentionally narrow and Phase-1-simple:

- accepts structured inputs for calendar, personal inbox, agent inbox, and Notion summary
- derives the `Yesterday` section from durable backend task state
- generates a recap with these sections:
  - `Yesterday`
  - `Today`
  - `Personal inbox`
  - `Agent inbox`
  - `Notion`
  - `Recommended next actions`
- stores the recap as a durable backend task plus a `daily_routine_recap` artifact
- prepares an email-ready payload for `franchieinc@gmail.com`
- creates conservative backend follow-up tasks for clearly actionable items
- optionally creates matching Notion tasks for new actionable items discovered outside Notion

This repo does not reimplement Gmail, calendar, cron scheduling, or outbound email send. Those remain on the OpenClaw side.

## Daily routine manual testing

Run the routine against a full payload file:

```bash
PYTHONPATH=src python3 -m agentic_os.cli daily-routine run \
  --input-file examples/daily_routine_sample.json
```

Run it with Notion creation disabled:

```bash
PYTHONPATH=src python3 -m agentic_os.cli daily-routine run \
  --input-file examples/daily_routine_sample.json \
  --no-notion
```

Or supply the sections individually as files or inline JSON:

```bash
PYTHONPATH=src python3 -m agentic_os.cli daily-routine run \
  --date 2026-03-21 \
  --timezone Europe/London \
  --calendar-file calendar.json \
  --personal-inbox-file personal_inbox.json \
  --agent-inbox-file agent_inbox.json \
  --notion-file notion.json \
  --no-notion
```

The command returns:

- the durable recap task
- the structured recap object
- the plain-text email body and outbound email payload
- any backend follow-up tasks created
- any matching Notion tasks created or skipped

Useful inspection commands after a run:

```bash
PYTHONPATH=src python3 -m agentic_os.cli task list --target daily_routine
PYTHONPATH=src python3 -m agentic_os.cli task list --target daily_routine_followup
PYTHONPATH=src python3 -m agentic_os.cli task show <task_id>
PYTHONPATH=src python3 -m agentic_os.cli audit tail --target daily_routine
```

## OpenClaw bridge runner

Use the bridge runner when OpenClaw has already gathered tool results and you want one call that:

- normalizes mixed/raw OpenClaw payloads into the backend daily-routine schema
- runs the durable daily-routine flow
- returns the email-ready recap payload and follow-up task results

CLI command:

```bash
PYTHONPATH=src python3 -m agentic_os.cli openclaw daily-routine \
  --input-file examples/openclaw_daily_routine_bridge_input.json \
  --print-normalized
```

Wrapper script (avoids manually setting `PYTHONPATH`):

```bash
python3 scripts/openclaw_daily_routine_bridge.py \
  --input-file examples/openclaw_daily_routine_bridge_input.json \
  --print-normalized
```

Use `--dry-run` to validate normalization only, without creating any backend records:

```bash
python3 scripts/openclaw_daily_routine_bridge.py \
  --input-file examples/openclaw_daily_routine_bridge_input.json \
  --dry-run
```

The bridge accepts either already-normalized sections or flat tool-style lists such as:

- `calendarSummary.items`
- `personalInboxSummary.messages`
- `agentInboxSummary.items`
- `notionSummary.items`

and maps them into the canonical `daily-routine run` shape.

## Notion intake bridge

Use the Notion sync command when OpenClaw heartbeat/cron should import manually created Notion tasks into durable backend state:

```bash
PYTHONPATH=src python3 -m agentic_os.cli notion sync-tasks \
  --status Inbox \
  --status Review \
  --updated-since 2026-03-21T00:00:00Z \
  --limit 50
```

Wrapper script (no manual `PYTHONPATH` required):

```bash
python3 scripts/openclaw_notion_sync_bridge.py \
  --status Inbox \
  --status Review \
  --limit 50
```

Behavior:

- creates one sync task (`target=notion_task_sync`) for the polling run
- imports unseen Notion items as backend capture tasks (`target=notion_task_sync_item`) linked by `external_ref=<notion_page_id>`
- deduplicates by existing `external_ref` first, then by `operation_key` if present
- keeps sync auditability with explicit `adapter_called` / `adapter_result` events

OpenClaw-side wiring still required (outside this repo):

- 8:30 AM cron trigger in `Europe/London`
- Gmail reads for personal inbox and agent inbox
- calendar reads for the day agenda
- optional Notion query read if OpenClaw owns that polling step
- sending the rendered recap email to `franchieinc@gmail.com`

Suggested cron command target:

```bash
cd /Users/dara/.openclaw/workspace/agentic-os && \
python3 scripts/openclaw_daily_routine_bridge.py --input-file /tmp/openclaw_daily_payload.json
```

## Week 4 flows

### 1. OpenClaw-backed Gmail read

This records the task, policy decision, tool metadata, tool result, optional artifact, summary, and completion.

```bash
PYTHONPATH=src python3 -m agentic_os.cli openclaw read \
  --domain personal \
  --risk low \
  --target gmail_thread \
  --metadata-json '{"thread_id":"thr_001"}' \
  --tool-name openclaw.gmail.read_thread \
  --tool-input-json '{"thread_id":"thr_001"}' \
  --tool-result-json '{"messages":[{"from":"teammate@example.com","subject":"Project update"}]}' \
  --artifact-type gmail_thread_snapshot \
  --artifact-json '{"messages":[{"from":"teammate@example.com","subject":"Project update"}]}' \
  --request "Summarize the latest Gmail thread from Alex" \
  --summary "Recorded Gmail thread summary for daily review."
```

### 2. OpenClaw-backed Notion draft

This records the task and stores the OpenClaw-generated draft as an artifact. Revisions still go through `artifact revise`.

```bash
PYTHONPATH=src python3 -m agentic_os.cli openclaw draft \
  --domain personal \
  --risk medium \
  --target notion_page_comment \
  --metadata-json '{"page_id":"pg_001"}' \
  --tool-name openclaw.notion.prepare_comment \
  --tool-input-json '{"page_id":"pg_001"}' \
  --artifact-type notion_comment_draft \
  --artifact-text "Draft comment for the Notion task update." \
  --request "Draft a Notion comment for the product task page" \
  --summary "Created Notion comment draft for review."

PYTHONPATH=src python3 -m agentic_os.cli artifact revise task_000002 \
  --artifact-text "Revised Notion comment with a clearer next step."
```

### 3. OpenClaw-backed approval and execute

This stores the task and approval request first:

```bash
PYTHONPATH=src python3 -m agentic_os.cli openclaw execution \
  --domain personal \
  --risk high \
  --target gmail_send_message \
  --metadata-json '{"to":["alex@example.com"],"subject":"Re: Project update"}' \
  --tool-name openclaw.gmail.send_message \
  --tool-input-json '{"to":["alex@example.com"],"subject":"Re: Project update"}' \
  --operation-key gmail-send-001 \
  --artifact-type outbound_email \
  --artifact-text "Approved reply body for Alex." \
  --request "Send the approved Gmail reply to Alex" \
  --result-summary "Gmail send completed."
```

Approve it:

```bash
PYTHONPATH=src python3 -m agentic_os.cli approval approve apr_<approval_id> \
  --note "Looks correct. Proceed."
```

Record execution once:

```bash
PYTHONPATH=src python3 -m agentic_os.cli task execute task_000003 \
  --tool-name openclaw.gmail.send_message \
  --tool-result-json '{"message_id":"msg_123"}' \
  --result-summary "OpenClaw sent the approved Gmail reply."
```

Rejected or duplicate execute attempts remain clear:

```bash
PYTHONPATH=src python3 -m agentic_os.cli task execute task_000003 \
  --tool-name openclaw.gmail.send_message \
  --tool-result-json '{"message_id":"msg_123"}' \
  --result-summary "Duplicate execute should no-op."
```

### 4. Inspect operator views

```bash
PYTHONPATH=src python3 -m agentic_os.cli task show task_000003
PYTHONPATH=src python3 -m agentic_os.cli approval show apr_<approval_id>
PYTHONPATH=src python3 -m agentic_os.cli execution show gmail-send-001
PYTHONPATH=src python3 -m agentic_os.cli audit tail --limit 10
```

These views show the task row, `action_source`, artifact history, approvals, execution record, rejection events, and enriched audit events.

### 5. Filter tasks and recap durable state

```bash
PYTHONPATH=src python3 -m agentic_os.cli task list \
  --status completed \
  --domain personal \
  --target gmail_send_message \
  --action-source openclaw_tool

PYTHONPATH=src python3 -m agentic_os.cli recap today
PYTHONPATH=src python3 -m agentic_os.cli recap approvals
PYTHONPATH=src python3 -m agentic_os.cli recap drafts
PYTHONPATH=src python3 -m agentic_os.cli recap failures
PYTHONPATH=src python3 -m agentic_os.cli recap external-actions
```

### 6. Future custom-adapter seam

```bash
PYTHONPATH=src python3 -m agentic_os.cli adapter execute \
  --adapter-name future_mail \
  --action-name send
```

Expected result: a clear not-implemented error. No fake provider integration is included in Week 4.
