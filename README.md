# agentic-os

Week 4 local-first backend for an OpenClaw-first agentic OS.

The default architecture is intentionally boring:

- OpenClaw tools and skills perform reads, drafting, and execution when those capabilities already exist.
- `agentic-os` stores durable task state, policy decisions, artifact revisions, approvals, execution records, and audit history.
- Future custom adapters are an explicit escape hatch, not the default integration path.

This repo does not add a web server, provider SDKs, or a generalized plugin framework.

## Layout

- `src/agentic_os/` - backend package and CLI
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
- Keep custom adapters as a small future-only seam in [`src/agentic_os/adapters.py`](/Users/dara/.openclaw/workspace/agentic-os/src/agentic_os/adapters.py).

The adapter seam currently raises a clear `CustomAdapterNotImplementedError`. That is intentional.

## Quick start

Run from the repo root with `PYTHONPATH=src`.

```bash
PYTHONPATH=src python3 -m agentic_os.cli init
PYTHONPATH=src python3 -m agentic_os.cli --help
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
