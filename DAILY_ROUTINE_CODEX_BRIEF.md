# Codex Brief — Phase 1 Daily Routine Flow

## Mission
Implement a **fully functional Phase 1 daily routine flow** that uses:
- **OpenClaw** as the conversational/control layer
- **existing OpenClaw Gmail + calendar tools** for reads
- **agentic-os** as the durable backend for recap/task/audit state
- **Notion** as the task/planning surface
- **agent inbox** as the operational mailbox

This must stay aligned with the revised Phase 1 architecture:
- no orchestration engine
- no separate planner runtime
- no workflow system sprawl
- OpenClaw does the interpretation, summaries, and tool use
- backend records durable state and thin integrations

## User-specific requirements

### Mailbox model
- **Personal inbox:** read + draft only access
  - use for summarizing, identifying important threads, and spotting reply-needed items
  - do not design this flow around sending from the personal inbox
- **Agent inbox:** `franchieinc@gmail.com`
  - this is the operational mailbox
  - recap email should be sent **only** to the agent inbox
  - agent inbox should be the target for automation-oriented mail workflows

### Daily recap delivery
- Send a **daily recap email** to `franchieinc@gmail.com`
- Delivery time: **8:30 AM daily**
- Time zone: **Europe/London**

### Notion behavior
- Automatic Notion task creation should happen for clearly actionable items discovered by the routine
- Use the already configured Notion adapter / config in this repo

## Existing context you should assume
- OpenClaw already has Gmail tools available
- OpenClaw already has calendar tools available
- A Notion adapter now exists in this repo and has been live-tested successfully for query/create
- The repo already has dashboard, durable tasks, audit, artifacts, approvals, and recap primitives

Read these first before changing code:
- `/Users/dara/.openclaw/workspace/revised-phase1-architecture.md`
- `/Users/dara/.openclaw/workspace/PHASE1_OPENCLAW_CLASSIFICATION_ROUTING_PLAN.md`
- `/Users/dara/.openclaw/workspace/PHASE1_NOTION_PLAN.md`
- `/Users/dara/.openclaw/workspace/agentic-os/README.md`

## Product goal
Support this daily routine:
1. At 8:30 AM, gather yesterday recap + today agenda + inbox priorities + Notion task state
2. Produce a structured daily recap
3. Send that recap as an email to the agent inbox (`franchieinc@gmail.com`)
4. Create backend durable records for the recap and any generated follow-up artifacts/tasks
5. Automatically create Notion tasks for clearly actionable items
6. Keep the whole thing Phase-1-simple and inspectable

## Scope

### Implement now
Build the smallest coherent daily-routine layer that includes:

#### 1. Daily routine runner / service method(s)
A service path that can:
- collect recap input from durable backend state
- accept external input payloads for:
  - calendar summary
  - personal inbox summary
  - agent inbox summary
  - Notion task summary
- generate/store a recap artifact or durable summary
- create follow-up backend tasks for actionable items
- create matching Notion tasks where appropriate

Important: because Gmail/calendar reads already happen in OpenClaw, the repo does **not** need to reimplement provider integrations here. It should accept structured inputs and record/act on them.

#### 2. A structured recap model
The recap should support these sections:
- `Yesterday`
  - completed
  - blocked
  - still open
- `Today`
  - calendar events
  - constraints / prep needed
- `Personal inbox`
  - urgent
  - needs reply
  - important FYI
- `Agent inbox`
  - operational items
  - alerts / automation intake
- `Notion`
  - Inbox
  - Review
  - Planned
  - blocked/stale if available
- `Recommended next actions`
  - top 3–5 actions

This can be represented as a typed structure plus a plain-text email rendering.

#### 3. Email-ready recap rendering
Produce a compact email body suitable for sending to `franchieinc@gmail.com`.
The repo does not need to directly send via Gmail API if that should remain in OpenClaw; but it must provide a clean rendered output / artifact / payload that OpenClaw can send.

If it is clean and consistent with the current architecture, adding a thin “prepare outbound recap email payload” path is encouraged.

#### 4. Action extraction / follow-up creation
When the recap identifies obvious actionable work, create:
- backend task(s)
- Notion task(s) for actionable items

Keep this conservative. Do not invent a giant planner. Only create tasks for clearly actionable items.

#### 5. CLI/testing surface
Add a small manual CLI path for local testing of the daily routine flow.
This can accept JSON input files or inline JSON strings for:
- calendar summary
- personal inbox summary
- agent inbox summary
- Notion summary

The goal is to make local/manual verification possible without wiring OpenClaw directly yet.

#### 6. Docs
Update `README.md` with:
- what the daily routine flow does
- how it fits the Phase 1 architecture
- how to run/test it manually
- what still needs to be wired in OpenClaw cron/tooling

### Optional if clean
- config for recap delivery metadata like recipient and schedule defaults
- a sample payload file for daily routine testing

## Do NOT build
- no separate orchestration engine
- no autonomous planner runtime
- no custom Gmail/calendar provider client in this repo if OpenClaw already has those tools
- no giant NLP/action-extraction subsystem
- no new UI for this task
- no long-running worker/daemon system

## Design guidance

### 1. Keep OpenClaw as the outer control plane
This repo should support the flow, not replace OpenClaw.
OpenClaw should still be responsible for:
- reading Gmail/calendar via its existing tools
- invoking the routine
- optionally sending the recap email through the agent inbox

### 2. Keep the repo responsible for durable state
This repo should:
- store recap artifact/summary
- store generated follow-up tasks
- update Notion tasks
- append audit events

### 3. Inputs should be structured, not provider-specific blobs
Define simple normalized inputs for:
- calendar events
- inbox summaries
- notion task summaries

Do not tightly couple the code to raw provider responses if avoidable.

### 4. Action creation should be conservative
Only create follow-up tasks when the item is clearly actionable.
Examples:
- explicit deadline / ask in email
- meeting prep needed
- blocked Notion item needing movement
- operational item in agent inbox requiring follow-up

## Suggested implementation shape
Adjust naming if needed, but keep it coherent with repo style.

Potential additions:
- `src/agentic_os/daily_routine.py`
- service integration in `src/agentic_os/service.py`
- CLI additions in `src/agentic_os/cli.py`
- maybe helper/sample files under repo root or `examples/`

## Required behavior

### A. Build recap from structured inputs
Given normalized inputs plus current backend state, generate:
- structured recap object
- plain-text recap email body
- durable summary/artifact/task result

### B. Record the recap durably
Store the recap as durable backend state so it can be audited and referenced later.
Use existing artifact/task/audit patterns where possible.

### C. Create follow-up tasks
For each clearly actionable item, create a backend task and, where appropriate, a Notion task via the existing adapter.

### D. Support the exact output sections
The email/text recap should include:
- Yesterday
- Today
- Personal inbox
- Agent inbox
- Notion
- Recommended next actions

### E. Respect the mailbox security split
The design/docs/examples should preserve:
- personal inbox = read/draft only
- agent inbox = operational send/receive mailbox

## Suggested manual verification path
Provide a way to run something like:
- daily routine with sample JSON input
- inspect produced recap text
- inspect created backend tasks / notion tasks

If direct live OpenClaw integration is not part of this coding task, that is fine — but the repo should be ready for it.

## Deliverables
Produce:
- daily routine implementation in-repo
- any necessary service/CLI/config additions
- docs updates
- sample/test input path if useful
- concise summary of files changed and how to run it

## Final instruction
Implement the smallest coherent Phase 1 daily-routine support layer for this repo: structured recap generation, durable storage, conservative actionable-task extraction, and Notion task creation, while leaving Gmail/calendar reads and actual cron/email-send control to OpenClaw.
