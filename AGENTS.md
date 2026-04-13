# AGENTIC-OS AGENT RUNTIME

Single source of truth for agent execution. The Paperclip skill is an API/governance reference. The agentic-os-bridge skill covers audit and approval CLI commands.

## 1. Wake Context

Paperclip injects env vars on every wake:

| Env Var | Meaning |
|---------|---------|
| `PAPERCLIP_TASK_ID` | Paperclip issue UUID that triggered this wake — always present. |
| `PAPERCLIP_WAKE_REASON` | Why triggered: `issue_assigned`, `issue_commented`, `issue_comment_mentioned`. |
| `PAPERCLIP_WAKE_COMMENT_ID` | Present on comment-triggered wakes — the comment UUID. |

**Comment-triggered wakes** (`PAPERCLIP_WAKE_COMMENT_ID` set):
- Fetch the specific comment first: `GET /api/issues/{issueId}/comments/{commentId}`.
- Do NOT work the issue from scratch.
- If the comment explicitly asks you to take the task, self-assign via checkout.
- If the comment asks for input/review but not ownership, respond in comments, then continue with assigned work.
- If the comment does not direct you to take ownership, do not self-assign.

## 2. Execution Loop

Follow these steps every wake:

1. **Read wake context.** Handle comment-trigger first if `PAPERCLIP_WAKE_COMMENT_ID` is set.

2. **Get assignments.** Use `GET /api/agents/me/inbox-lite`. Work `in_progress` first, then `todo`. Skip `blocked` unless you can unblock it. If `PAPERCLIP_TASK_ID` is set and assigned to you, prioritize it.
   - **Blocked-task dedup:** If your most recent comment was a blocked-status update AND no new comments since, skip entirely.

3. **Checkout.** Always checkout before doing any work:
   ```
   POST /api/issues/{issueId}/checkout
   Headers: Authorization: Bearer $PAPERCLIP_API_KEY, X-Paperclip-Run-Id: $PAPERCLIP_RUN_ID
   { "agentId": "{your-agent-id}", "expectedStatuses": ["todo", "backlog", "blocked"] }
   ```
   409 Conflict = owned by another agent. **Never retry a 409.**

4. **Resolve backend task_id.** Use this deterministic order, stop at first success:
   ```bash
   RUNTIME_ID="${PAPERCLIP_AGENT_ID:-${PAPERCLIP_RUN_ID:-task_executor_cron}}"
   ```
   - If `PAPERCLIP_TASK_ID` starts with `task_`, use it directly.
   - Otherwise resolve via CLI:
     ```bash
     cd /Users/dara/agents/agentic-os
     PYTHONPATH=src python3 -m agentic_os.cli task resolve-by-paperclip-issue \
       --paperclip-issue-id "$PAPERCLIP_TASK_ID"
     ```
   - If `PAPERCLIP_TASK_ID` is absent:
     ```bash
     cd /Users/dara/agents/agentic-os
     PYTHONPATH=src python3 -m agentic_os.cli task list-ready --limit 1
     ```

5. **Claim.** Run pickup once for the resolved `task_id`:
   ```bash
   cd /Users/dara/agents/agentic-os
   PYTHONPATH=src python3 -m agentic_os.cli task pickup --task-id <task_id> --claimed-by "$RUNTIME_ID"
   ```
   - `{"success": true}` → continue.
   - `{"success": false, "reason": "already_claimed"}` → check `claimed_by`. If it matches `$RUNTIME_ID`, continue under existing claim. Otherwise drop task and stop.

6. **Dispatch.** Mark dispatched once after successful claim:
   ```bash
   cd /Users/dara/agents/agentic-os
   PYTHONPATH=src python3 -m agentic_os.cli task mark-dispatched --task-id <task_id> --session-key <session_key> --agent <agent_name>
   ```

7. **Read brief.** Read the `agentic-os brief` document on the Paperclip issue. Step 4's resolution triggers import and brief generation for both backend-first and Paperclip-first issues — the brief is always present by this step.
   - Extract `task_id`, domain, mode, and writeback instructions from the brief.
   - If the brief is unexpectedly absent, the import in Step 4 failed — comment that the brief is pending and exit.

8. **Resolve mode from the brief:**
   - `PLANNING INSTRUCTIONS` → `plan`
   - `== APPROVED EXECUTION PLAN ==` → execute approved plan
   - Otherwise → `execute` (direct)

9. **Do the work.** Execute the assigned scope for your role.

10. **Submit results.** Follow the writeback instructions in the brief — it specifies the exact submission mechanism and file format for the backend callback.
    - **Structured output** (reasoning, sections, >150 words, needs versioning): use the **brief-system skill** to produce the document with proper frontmatter, versioned filename, and doc-type routing before submission.
    - **Inline comment** (≤3 bullets, status update, confirmation): post directly as a Paperclip comment — no document or submission needed.

11. **Update Paperclip and exit.**
    - Always comment on `in_progress` work before exiting (except blocked tasks with no new context).
    - If blocked, `PATCH` status to `blocked` with a blocker comment before exiting.
    - If done for this heartbeat but task is not complete, release: `POST /api/issues/{issueId}/release`.
    - Record memory updates in your agent home (`memory/`, `life/`, `MEMORY.md`).

## 3. Paperclip API Essentials

Auth env vars auto-injected: `PAPERCLIP_AGENT_ID`, `PAPERCLIP_COMPANY_ID`, `PAPERCLIP_API_URL`, `PAPERCLIP_API_KEY`, `PAPERCLIP_RUN_ID`. All requests use `Authorization: Bearer $PAPERCLIP_API_KEY`. Include `X-Paperclip-Run-Id: $PAPERCLIP_RUN_ID` on all mutating requests.

| Action | Endpoint |
|--------|----------|
| Checkout | `POST /api/issues/{issueId}/checkout` |
| Update status/comment | `PATCH /api/issues/{issueId}` |
| Release | `POST /api/issues/{issueId}/release` |
| Compact inbox | `GET /api/agents/me/inbox-lite` |
| Heartbeat context | `GET /api/issues/{issueId}/heartbeat-context` |
| Comments | `GET /api/issues/{issueId}/comments` |
| Add comment | `POST /api/issues/{issueId}/comments` |

Full endpoint table, governance, and API reference → **paperclip skill**.

## 4. agentic-os CLI Quick Reference

- Working directory: `/Users/dara/agents/agentic-os`
- Invocation: `PYTHONPATH=src python3 -m agentic_os.cli <command>`

| Command | Purpose |
|---------|---------|
| `task resolve-by-paperclip-issue --paperclip-issue-id <id>` | Resolve backend task from Paperclip issue |
| `task list-ready --limit N` | List tasks eligible for execution |
| `task pickup --task-id <id> --claimed-by <runtime_id>` | Claim task |
| `task mark-dispatched --task-id <id> --session-key <key> --agent <name>` | Record dispatch |
| `task record-result --task-id <id> --output-file <path> --session-key <key>` | Submit result |
| `task submit-plan --task-id <id> --plan-file <path> --session-key <key>` | Submit plan |

Audit, approval, triage, and health commands → **agentic-os-bridge skill**.

## 5. Callback Identity

Use backend `task_id` (from the brief or resolved in Step 4) as the callback identifier for all submissions. Paperclip issue ID is contextual metadata only — never pass it to `submit-result`.

Successful responses: `RESULT_SUBMITTED`, `ALREADY_SUBMITTED`, `PLAN_SUBMITTED`.
Recovery: `WRITEBACK_FAILED: <task_id> <reason>` → trigger incident remediation.

## 6. Critical Rules

- **Always checkout** before working. Never PATCH to `in_progress` manually.
- **Never retry a 409.** The task belongs to someone else.
- **Never look for unassigned work.**
- **Self-assign only for explicit @-mention handoff** with `PAPERCLIP_WAKE_COMMENT_ID` set. Use checkout, never direct assignee patch.
- **Honor "send it back to me"** from board users — reassign with `assigneeAgentId: null` and `assigneeUserId`, status `in_review`.
- **Always comment** on `in_progress` work before exiting (except blocked tasks with no new context).
- **Always set `parentId`** on subtasks (and `goalId` unless creating top-level work).
- **Never cancel cross-team tasks.** Reassign to manager with a comment.
- **Budget**: auto-paused at 100%. Above 80%, critical tasks only.
- **Escalate** via `chainOfCommand` when stuck.
- **Commit co-author**: `Co-Authored-By: Paperclip <noreply@paperclip.ing>` on all git commits.
- **Comment style**: concise markdown, ticket references as links (`[PAP-224](/PAP/issues/PAP-224)`), company-prefixed URLs required.

## 7. Agent Homes

| Agent | Home |
|-------|------|
| `chief_of_staff` | `/Users/dara/agents/chief-of-staff/` |
| `project_manager` | `/Users/dara/agents/project-manager/` |
| `engineering_manager` | `/Users/dara/agents/engineering-manager/` |
| `engineer` | `/Users/dara/agents/engineering/claude/` |
| `executor_codex` | `/Users/dara/agents/engineering/codex/` |
| `infrastructure_engineer` | `/Users/dara/agents/infrastructure-engineer/codex/` |
| `content_writer` | `/Users/dara/agents/content-writer/` |
| `accountant` | `/Users/dara/agents/accountant/` |
| `executive_assistant` | `/Users/dara/agents/executive-assistant/` |

## 8. Role Files

Each role keeps only role-specific behavior. Shared runtime stays here.

## 9. Contribution Work Selection And Repo Strategy Memory

Technical contribution routing is:

1. Chief of Staff creates a contribution-management task for `project_manager`.
2. Project Manager reviews the portfolio and repo strategy memory.
3. Project Manager either:
   - creates a fresh executor task for `engineer` or `executor_codex`
   - comments on the same Paperclip issue to wake the current assignee for PR follow-up
   - escalates to `engineering_manager`

When selecting an open-source issue (repos: `paperclipai/paperclip`, `openclaw/openclaw`, `go-playground/validator`, `ktorio/ktor`), consult the contribution-priority scoreboard first.

- Latest JSON: `/Users/dara/agents/projects/system/scoreboard/scoreboard.json`
- Latest Markdown: `/Users/dara/agents/projects/system/scoreboard/scoreboard.md`

**Refresh** (from `technical/` root) if older than 24 hours:
```bash
python3 scripts/contribution-scoreboard.py \
  --output /Users/dara/agents/projects/system/scoreboard/scoreboard.json
```

**Selection rule:** Pick the highest-ranked issue in a repo where you have an active fork and recent context. Confirm the issue is still open and unassigned before starting.

Repo strategy memory files live at:

- `/Users/dara/agents/projects/technical/repo-strategy/paperclip.md`
- `/Users/dara/agents/projects/technical/repo-strategy/openclaw.md`
- `/Users/dara/agents/projects/technical/repo-strategy/validator.md`
- `/Users/dara/agents/projects/technical/repo-strategy/ktor.md`

These files store durable repo guidance only. Live PR state must still be fetched from GitHub on each run.
