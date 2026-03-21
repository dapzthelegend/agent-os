# OpenClaw ↔ agentic-os bridge (Phase 1)

This repo now exposes a concrete bridge command for the daily routine path:

```bash
python3 scripts/openclaw_daily_routine_bridge.py --input-file <payload.json>
```

It performs two steps:

1. normalize OpenClaw-collected payloads into the canonical `daily-routine run` input shape
2. execute `agentic-os` daily routine storage/follow-up creation

## Minimal command contract

Use one of:

- `--input-file <path>`
- `--input-json '<json object>'`

Optional flags:

- `--date YYYY-MM-DD`
- `--timezone Europe/London`
- `--recipient franchieinc@gmail.com`
- `--delivery-time 08:30`
- `--no-notion`
- `--print-normalized`
- `--dry-run` (normalization only, no backend writes)

## Input payload (OpenClaw side)

The bridge accepts either normalized sections or common tool-output shapes.

Supported section aliases:

- `calendar` or `calendarSummary`
- `personal_inbox` or `personalInboxSummary`
- `agent_inbox` or `agentInboxSummary`
- `notion` or `notionSummary`

Examples of accepted flat lists:

- `calendarSummary.items`
- `personalInboxSummary.messages`
- `agentInboxSummary.items`
- `notionSummary.items`

An end-to-end sample is included at:

- `examples/openclaw_daily_routine_bridge_input.json`

## Suggested OpenClaw runtime flow

1. Gather tool outputs (calendar, personal inbox summary, agent inbox summary, optional Notion summary)
2. Write one payload JSON file
3. Execute bridge command
4. Read command JSON output:
   - send `email_payload.body_text` to `email_payload.to` using agent inbox send path
   - retain `task.id` and follow-up ids for auditability

## Scheduling (OpenClaw cron)

Target schedule:

- 8:30 AM daily
- timezone: `Europe/London`

Cron target command:

```bash
cd /Users/dara/.openclaw/workspace/agentic-os && python3 scripts/openclaw_daily_routine_bridge.py --input-file /tmp/openclaw_daily_payload.json
```
