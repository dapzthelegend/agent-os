# Codex Brief — Wire OpenClaw for the Daily Routine Flow

## Mission
Implement the remaining **OpenClaw-side wiring** needed to make the daily routine actually run in practice.

The backend support layer in `agentic-os` now exists. What remains is the OpenClaw-side integration so that:
- OpenClaw gathers the daily inputs
- normalizes them into the shape expected by `agentic-os`
- invokes the daily routine backend path
- sends the rendered recap email through the agent inbox
- schedules the whole thing for **8:30 AM Europe/London daily**

## User requirements
- **Agent inbox:** `franchieinc@gmail.com`
- **Personal inbox:** read + draft only access; do not design sending around the personal inbox
- **Daily recap delivery:** send recap email **only** to `franchieinc@gmail.com`
- **Time:** 8:30 AM daily, Europe/London
- **Automatic Notion task creation:** yes
- Use the recap sections/items already agreed and implemented in the repo’s daily-routine layer

## Existing state
Already implemented in `agentic-os`:
- Notion adapter
- daily routine support layer
- CLI/manual testing path
- sample payload
- durable recap + follow-up task creation

Already available in OpenClaw environment:
- Gmail tools
- calendar tools
- cron
- exec/read/write/edit

## Read first
Before coding, read:
- `/Users/dara/.openclaw/workspace/revised-phase1-architecture.md`
- `/Users/dara/.openclaw/workspace/PHASE1_OPENCLAW_CLASSIFICATION_ROUTING_PLAN.md`
- `/Users/dara/.openclaw/workspace/PHASE1_NOTION_PLAN.md`
- `/Users/dara/.openclaw/workspace/agentic-os/DAILY_ROUTINE_CODEX_BRIEF.md`
- `/Users/dara/.openclaw/workspace/agentic-os/README.md`
- inspect the implemented daily-routine CLI/service in `agentic-os`
- inspect current `~/.openclaw/openclaw.json`

## Product goal
After this work, the system should support a real automated daily flow:
1. At 8:30 AM Europe/London, OpenClaw runs the daily routine
2. It gathers:
   - yesterday/backend recap context
   - today’s calendar summary
   - personal inbox summary (read-only usage)
   - agent inbox summary (`franchieinc@gmail.com`)
   - Notion task summary if needed on the OpenClaw side
3. It normalizes that into the input shape expected by `agentic-os`
4. It invokes the `agentic-os` daily-routine path
5. It obtains the rendered recap output
6. It sends the recap email to `franchieinc@gmail.com`
7. It records/keeps the process inspectable and aligned with Phase 1

## Scope

### Implement now
Build the smallest coherent OpenClaw-side wiring for the daily routine.

This likely includes:

#### 1. A local runner script or equivalent glue
A small local script/module that:
- runs under this workspace/host
- collects data from OpenClaw-reachable sources and/or accepts them from OpenClaw tool calls
- builds the normalized payload for `agentic-os`
- invokes the `agentic-os` daily-routine CLI/service
- extracts the rendered recap content for email sending

Prefer boring local glue over a giant framework.

#### 2. A clear OpenClaw invocation path
Make it straightforward for OpenClaw to trigger the routine.
This could be via:
- a workspace script invoked by `exec`
- a small custom skill
- or another simple, local integration path

Pick the simplest option that fits OpenClaw and is easy to inspect.

#### 3. Scheduling / cron setup guidance
If safe to implement directly, wire the exact OpenClaw cron job needed.
If direct cron wiring from repo code is awkward, then produce the exact command/config instructions needed and document them clearly.

Preferred outcome: as much of the scheduling setup is implemented as reasonably possible without inventing new infrastructure.

#### 4. Email-send path for recap delivery
Use the operational/agent inbox path only.
Do not route recap sending through the personal inbox.

If OpenClaw’s Gmail tool surface requires the actual send to remain in OpenClaw rather than inside `agentic-os`, keep it there.

#### 5. Respect the security split
- personal inbox: read + draft only usage in this flow
- agent inbox: operational send/receive mailbox

#### 6. Docs / local notes
Update the relevant docs or local notes so future operation is obvious.
Potential places:
- `README.md` in `agentic-os` if needed
- workspace notes/docs if needed
- a dedicated wiring doc if helpful

## Do NOT build
- no orchestration engine
- no custom Gmail/calendar provider client replacing OpenClaw tools
- no second backend service
- no broad automation platform
- no invasive rewrite of the new daily-routine layer

## Design guidance

### Keep OpenClaw as the control plane
OpenClaw should still do the live tool reads and scheduling.
The repo should remain the durable backend.

### Prefer explicit normalized payloads
Do not pass raw provider blobs if avoidable.
Normalize into the daily-routine input shape expected by `agentic-os`.

### Make manual inspection easy
A future human should be able to inspect:
- what script/command runs
- what cron triggers it
- where email is sent
- where recap artifacts/tasks end up

## Deliverables
Produce:
- the OpenClaw-side wiring needed for the daily routine
- any helper script(s) or skill(s)
- any config/docs updates needed
- if possible, cron job setup or a precise ready-to-run cron configuration
- a concise summary of what changed and what still requires user/provider setup

## Final instruction
Implement the smallest coherent OpenClaw-side wiring so the Phase 1 daily routine can actually run at 8:30 AM Europe/London, using OpenClaw for Gmail/calendar/scheduling/email-send and `agentic-os` for durable recap/task/Notion state.
