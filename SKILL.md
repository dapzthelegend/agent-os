# AGENTIC-OS SKILLS WORKFLOW

Central skill routing for runtime task execution.

## Core Skill Sequence

1. `agentic-os-bridge` — resolve task from Paperclip env vars, claim, dispatch, persist result.
2. `submit-plan` — use when the brief is planning-gated (`plan` / `approve_plan`).
3. `submit-result` — use for direct execution (`execute`) or approved-plan execution.

## Mode-to-Skill Mapping

- `PLANNING INSTRUCTIONS` → write plan → `/submit-plan <task_id>`
- `== APPROVED EXECUTION PLAN ==` → execute approved plan → `/submit-result <task_id>`
- default execution brief → execute directly → `/submit-result <task_id>`

## Policy Outcomes (from backend)

- `execute` → execution brief (no approval gate)
- `plan` → planning brief
- `approve` → waits for approval before execution brief is issued
- `approve_plan` → planning brief first, then approval, then execution brief

## Identity Contract

`task_id` comes from the `agentic-os brief` document on the Paperclip issue.
Paperclip issue ID is reference-only — never pass it to `submit-result`.

## Output Files

- Plan file: `/tmp/task_plan_<task_id>.txt`
- Result file: `/tmp/task_result_<task_id>.txt`
