# agentic-os

Local-first backend for an OpenClaw-powered agentic OS. Handles durable task state, policy decisions, artifact revisions, approvals, execution records, and audit history.

OpenClaw tools and skills handle reads, drafting, and execution. This repo stores what happened and what should happen next.

## Layout

```
src/agentic_os/       backend package and CLI
src/agentic_os/web.py FastAPI dashboard entrypoint
src/agentic_os/templates/ server-rendered dashboard templates
data/                 SQLite database and JSONL audit log
artifacts/            versioned artifact files
```

## Quick start

```bash
PYTHONPATH=src python3 -m agentic_os.cli init
PYTHONPATH=src python3 -m agentic_os.cli --help
```

## Notion config

Copy `agentic_os.config.example.json` to `agentic_os.config.json` and fill in your values:

```json
{
  "notion": {
    "apiTokenEnv": "NOTION_API_KEY",
    "databaseId": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
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

Export the token before running Notion commands:

```bash
export NOTION_API_KEY="secret_..."
```

## Dashboard

Local operator console for tasks, approvals, artifacts, executions, and audit history.

```bash
PYTHONPATH=src python3 -m pip install -e .
PYTHONPATH=src uvicorn agentic_os.web:app --reload --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080`. Bind to loopback only — not intended for public exposure.

Pages: `/`, `/tasks`, `/tasks/{id}`, `/approvals`, `/approvals/{id}`, `/executions/{key}`, `/audit`, `/recaps`

JSON API: `/api/overview`, `/api/tasks`, `/api/approvals`, `/api/executions/{key}`, `/api/audit`, `/api/recap/{today|approvals|drafts|failures|external-actions}`

## Daily routine

Accepts structured inputs (calendar, personal inbox, agent inbox, Notion summary), generates a recap, stores it durably, and creates follow-up tasks.

```bash
PYTHONPATH=src python3 -m agentic_os.cli daily-routine run \
  --input-file examples/daily_routine_sample.json

# Via OpenClaw bridge script
python3 scripts/openclaw_daily_routine_bridge.py \
  --input-file examples/openclaw_daily_routine_bridge_input.json
```

Use `--dry-run` to validate normalization without creating records. Use `--no-notion` to skip Notion task creation.

## Notion sync

Import manually created Notion tasks into backend state:

```bash
python3 scripts/openclaw_notion_sync_bridge.py \
  --status Inbox --status Review --limit 50
```

## CLI reference

```bash
# Tasks
task list [--status] [--domain] [--target] [--action-source]
task show <task_id>
task execute <task_id> --tool-name ... --tool-result-json ...

# Approvals
approval approve <approval_id> --note "..."
approval show <approval_id>

# Notion
notion create-task --domain ... --risk ... --request ... --title ...
notion query-tasks [--status] [--updated-since] [--limit]
notion sync-tasks [--status] [--updated-since] [--limit]
notion get-task <page_id>
notion update-status <task_id> --backend-status ...
notion append-note <task_id> --note "..."

# Recaps
recap today|approvals|drafts|failures|external-actions

# Audit
audit tail [--limit] [--target]

# OpenClaw flows
openclaw read --domain ... --risk ... --target ... --tool-name ...
openclaw draft --domain ... --risk ... --target ... --tool-name ...
openclaw execution --domain ... --risk ... --target ... --operation-key ...
openclaw daily-routine --input-file ...
```
