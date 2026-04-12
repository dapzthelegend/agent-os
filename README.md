# agentic-os

Local-first backend for Paperclip-driven execution.

`agentic-os` owns durable task state, policy, approvals, callbacks, and audit.
Paperclip owns execution orchestration and runtime assignment.

## Layout

- `src/agentic_os/` backend package and CLI
- `src/agentic_os/web.py` FastAPI dashboard entrypoint
- `data/` SQLite database + JSONL audit log
- `artifacts/` versioned artifact files

## Quick start

```bash
PYTHONPATH=src python3 -m agentic_os.cli init
PYTHONPATH=src python3 -m agentic_os.cli --help
```

## Config

Copy `agentic_os.config.example.json` to `agentic_os.config.json` and fill the `paperclip` block.

## Dashboard

```bash
PYTHONPATH=src uvicorn agentic_os.web:app --reload --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080`.

## API Surface

- `/api/overview`
- `/api/tasks`
- `/api/approvals`
- `/api/executions/{operation_key}`
- `/api/audit`
- `/api/recap/today`
- `/api/recap/approvals`
- `/api/recap/awaiting-input`
- `/api/recap/failures`
- `/api/recap/external-actions`
- `/api/recap/overdue`
- `/api/recap/in-progress`

## CLI Highlights

```bash
# Tasks
task list [--status] [--domain] [--target] [--action-source]
task show <task_id>
task trace <task_id>
task list-ready [--limit]
task pickup --task-id ...
task mark-dispatched --task-id ... --session-key ... --agent ...
task record-result --task-id ... --output-file ...

# Recaps (audit convenience)
recap today|approvals|awaiting-input|failures|external-actions|overdue|in-progress

# Audit
audit tail [--limit] [--target]
```

## Boundary Rules

- No backend Gmail/Calendar polling.
- No backend daily recap trigger/execution pipeline.
- No Notion sync/execution flows.
- Discord remains the notification channel.
