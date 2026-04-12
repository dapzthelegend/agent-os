"""Dispatch payload builder — creates structured ACP task briefs."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from .models import RequestClassification
from .storage import Database
from .config import Paths, default_paths


@dataclass(frozen=True)
class DispatchPayload:
    """Structured payload for ACP agent dispatch."""
    task_id: str
    paperclip_issue_id: Optional[str]
    routing: str                     # policy action (execute, plan, approve, approve_plan)
    agent: str                       # Paperclip agent key (e.g. engineer, project_manager)
    brief: str                       # full prompt string for ACP session
    timeout_seconds: int = 300
    fallback_agent: Optional[str] = None  # model to use if primary is unavailable


class Dispatcher:
    """Builds dispatch payloads from classified backend tasks."""

    # Brief length above which compression is attempted (Phase 3)
    _COMPRESS_THRESHOLD = 3000

    def __init__(self) -> None:
        pass

    def build_payload(
        self,
        task_id: str,
        paperclip_issue_id: Optional[str],
        title: str,
        classification: RequestClassification,
        routing: str,
        agent: str,
        fallback_agent: Optional[str] = None,
        approved_plan: Optional[str] = None,
        task_mode: Optional[str] = None,
        task_status: Optional[str] = None,
    ) -> DispatchPayload:
        """
        Build dispatch payload for a classified task.

        Args:
            task_id: backend task ID
            paperclip_issue_id: Paperclip issue ID (nullable)
            title: task title / description
            classification: RequestClassification
            routing: execute | plan | approve | approve_plan (from policy_engine)
            agent: Paperclip agent key (from routing rules or decision engine)
            approved_plan: approved plan text for plan_first tasks (included in brief)
            task_mode: 'plan_first' or 'direct'
            task_status: current task status (used to detect planning phase)

        Returns:
            DispatchPayload with full brief (compressed if very long)
        """
        brief = self._build_brief(
            title=title,
            task_id=task_id,
            domain=classification.domain,
            intent_type=classification.intent_type,
            risk_level=classification.risk_level,
            approved_plan=approved_plan,
            task_mode=task_mode,
            task_status=task_status,
            paperclip_issue_id=paperclip_issue_id,
        )

        # Phase 3: compress very long briefs before dispatch
        if len(brief) > self._COMPRESS_THRESHOLD:
            from .decision_engine import compress_brief as _compress_brief
            brief = _compress_brief(brief, max_chars=self._COMPRESS_THRESHOLD)

        # Timeouts are tuned by task ownership role, not model aliases.
        timeout_by_agent = {
            "chief_of_staff": 600,
            "engineering_manager": 600,
            "engineer": 600,
            "infrastructure_engineer": 600,
            "project_manager": 300,
            "executor_codex": 240,
            "content_writer": 300,
            "accountant": 300,
            "executive_assistant": 300,
        }
        timeout = timeout_by_agent.get(agent, 300)

        return DispatchPayload(
            task_id=task_id,
            paperclip_issue_id=paperclip_issue_id,
            routing=routing,
            agent=agent,
            brief=brief,
            timeout_seconds=timeout,
            fallback_agent=fallback_agent,
        )

    def _writeback_instructions(self, task_id: str) -> str:
        return f"""
== MANDATORY WRITE-BACK ==
After outputting TASK_DONE: {task_id}, you MUST record the result:

1. Write your complete output to a temp file:
   OUTPUT_FILE="/tmp/task_result_{task_id}.txt"

2. Run the callback helper:
   /Users/dara/agents/bin/submit-result {task_id}

3. Verify exit code 0 and output `RESULT_SUBMITTED` (or `ALREADY_SUBMITTED`). On failure output:
   WRITEBACK_FAILED: {task_id} <error>
   and stop.
"""

    def _build_brief(
        self,
        title: str,
        task_id: str,
        domain: str,
        intent_type: str,
        risk_level: str,
        approved_plan: Optional[str] = None,
        task_mode: Optional[str] = None,
        task_status: Optional[str] = None,
        paperclip_issue_id: Optional[str] = None,
    ) -> str:
        """Build the full prompt brief for an ACP session."""

        # Plan-first tasks in the planning phase get a planning brief, not an execution brief
        if task_mode == "plan_first" and task_status == "to_do":
            return self._build_planning_brief(title, task_id, paperclip_issue_id or "")

        if intent_type == "content":
            brief = self._build_content_brief(title, task_id, risk_level)
        elif domain == "personal" and intent_type == "draft":
            brief = self._build_draft_brief(title, task_id, domain, risk_level)
        elif domain == "technical" and intent_type in ("execute", "read"):
            brief = self._build_technical_brief(title, task_id, domain, intent_type, risk_level)
        else:
            brief = self._build_generic_brief(title, task_id, domain, intent_type, risk_level)

        if approved_plan:
            plan_section = (
                f"\n== APPROVED EXECUTION PLAN ==\n"
                f"The following plan has been reviewed and approved by the operator.\n"
                f"You MUST follow this plan. Do not deviate without explicit instruction.\n\n"
                f"{approved_plan.strip()}\n"
                f"== END OF PLAN ==\n"
            )
            # Insert the plan block right before the write-back instructions
            writeback_marker = "== MANDATORY WRITE-BACK =="
            if writeback_marker in brief:
                idx = brief.index(writeback_marker)
                brief = brief[:idx] + plan_section + "\n" + brief[idx:]
            else:
                brief = brief + plan_section

        return brief

    def _build_planning_brief(self, title: str, task_id: str, paperclip_issue_id: str) -> str:
        """
        Planning brief for the execution agent on a plan_first task.

        The execution agent (engineer, content writer, etc.) writes the plan.
        The Project Manager reviews it and approves or requests revisions.
        The agent does NOT execute until the plan is approved.
        """
        return f"""You have been assigned a task that requires a plan before execution.

Task: {title}

CALLBACK IDENTITY
=================
Your task ID is: {task_id}
This is the ONLY identifier you pass to submit-plan and submit-result.
Do NOT use the Paperclip issue ID as the task identifier.

Paperclip issue (for context only): {paperclip_issue_id or "none"}

PLANNING INSTRUCTIONS
=====================
Before doing any work, you must write a plan and submit it for review.
The Project Manager will review your plan and either approve it or request revisions.
Do not begin execution until you receive an approved plan back.

1. Write a structured plan document. Include:
   - Objective: what this task is trying to achieve
   - Approach: your recommended strategy
   - Steps: ordered, concrete action items
   - Risks: what could go wrong and how to mitigate
   - Expected output: what a successful result looks like
   - Estimated effort: rough time/complexity estimate

2. Post the plan as a document on the Paperclip issue (issue ID: {paperclip_issue_id}).

3. Submit the plan to the backend for PM review:
   - Write your plan to /tmp/task_plan_{task_id}.txt
   - Run: /Users/dara/agents/bin/submit-plan {task_id} --doc-id <paperclip_document_id>

4. Stop here. The Project Manager will review and respond via Paperclip comment.
   If approved, you will be reassigned with the approved plan and execution instructions.
   If revision is requested, you will receive feedback and be asked to replan.
"""

    def _build_content_brief(self, title: str, task_id: str, risk_level: str) -> str:
        """
        Structured content brief for articles, docs, research summaries.

        Extracts topic, infers format and audience from the title so the
        agent has enough structured context to produce a well-shaped output.
        """
        topic = title.strip()
        # Infer format hint from title keywords
        title_lower = title.lower()
        if any(k in title_lower for k in ("blog", "post")):
            fmt = "blog post"
            length = "600–900 words"
        elif any(k in title_lower for k in ("article",)):
            fmt = "article"
            length = "800–1200 words"
        elif any(k in title_lower for k in ("summary", "summarise", "summarize")):
            fmt = "structured summary"
            length = "300–500 words"
        elif any(k in title_lower for k in ("report",)):
            fmt = "report"
            length = "500–800 words"
        elif any(k in title_lower for k in ("explainer",)):
            fmt = "explainer"
            length = "400–700 words"
        else:
            fmt = "document"
            length = "400–800 words"

        return f"""You are producing a content piece for Dara.

Topic: {topic}
Task ID: {task_id} (use this ID in ALL callbacks — never the Paperclip issue ID)
Format: {fmt}
Target length: {length}
Audience: Dara (operator/founder, technically literate, no fluff)
Risk: {risk_level}

Output requirements:
- Write in markdown
- Use clear headings (##, ###)
- No filler, no padding — every sentence must earn its place
- Conclude with a short "Key takeaways" section (3–5 bullet points)

When done, output your result in this exact format:
RESULT_START
{{your markdown content here}}
RESULT_END
TASK_DONE: {task_id}
""" + self._writeback_instructions(task_id)

    def _build_draft_brief(
        self, title: str, task_id: str, domain: str, risk_level: str
    ) -> str:
        return f"""You are working on a task assigned by Dara.

Task: {title}
Task ID: {task_id} (use this ID in ALL callbacks — never the Paperclip issue ID)
Domain: {domain}
Risk: {risk_level}

Instructions:
- Complete the task as described. Be thorough and high-quality.
- When done, output your result in this exact format:
  RESULT_START
  {{your output here}}
  RESULT_END
- Then output: TASK_DONE: {task_id}

Style: concise, direct, no filler. Dara's preference.
""" + self._writeback_instructions(task_id)

    def _build_technical_brief(
        self, title: str, task_id: str, domain: str, intent_type: str, risk_level: str
    ) -> str:
        return f"""You are working on a technical task assigned by Dara.

Task: {title}
Task ID: {task_id} (use this ID in ALL callbacks — never the Paperclip issue ID)
Domain: {domain}
Intent: {intent_type}
Risk: {risk_level}
Workspace: ~/.openclaw/workspace/

Instructions:
- Read relevant files before writing. Understand before acting.
- For code: write clean, minimal, well-commented code.
- When done, output your result in this exact format:
  RESULT_START
  {{your output here}}
  RESULT_END
- Then output: TASK_DONE: {task_id}
""" + self._writeback_instructions(task_id)

    def _build_generic_brief(
        self, title: str, task_id: str, domain: str, intent_type: str, risk_level: str
    ) -> str:
        return f"""You are working on a task assigned by Dara.

Task: {title}
Task ID: {task_id} (use this ID in ALL callbacks — never the Paperclip issue ID)
Domain: {domain}
Intent: {intent_type}
Risk: {risk_level}

Instructions:
- Complete the task as described.
- Be thorough and high-quality.
- When done, output your result in this exact format:
  RESULT_START
  {{your output here}}
  RESULT_END
- Then output: TASK_DONE: {task_id}

Style: concise, direct, no filler.
""" + self._writeback_instructions(task_id)


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Build dispatch payload for a task")
    parser.add_argument("--task-id", required=True, help="Backend task ID")
    args = parser.parse_args()

    paths = default_paths()
    db = Database(paths.db_path)
    db.initialize()

    # Load task from database
    try:
        task = db.get_task(args.task_id)
    except KeyError:
        print(json.dumps({"error": f"Task {args.task_id} not found"}))
        return

    # Build dispatch payload
    dispatcher = Dispatcher()
    classification = RequestClassification(
        domain=task.domain,
        intent_type=task.intent_type,
        risk_level=task.risk_level,
    )
    # Agent is stored in request_metadata at creation time; policy_decision is the routing
    routing = task.policy_decision or "execute"
    metadata = json.loads(task.request_metadata_json) if task.request_metadata_json else {}
    agent = metadata.get("agent", "project_manager")

    # Load approved plan for plan_first tasks
    approved_plan: Optional[str] = None
    if task.task_mode == "plan_first" and task.approved_plan_revision_id:
        from pathlib import Path
        from .artifacts import ArtifactStore
        import json as _json
        try:
            store = ArtifactStore(paths.artifacts_dir)
            plan_artifacts = [
                a for a in db.list_artifacts(task.id)
                if a.get("artifact_type") == "plan_document"
            ]
            if plan_artifacts:
                latest = sorted(plan_artifacts, key=lambda a: a.get("version", 0))[-1]
                raw = store.read_text(latest["path"])
                parsed = _json.loads(raw)
                approved_plan = parsed.get("plan_text") or raw
        except Exception:
            pass

    payload = dispatcher.build_payload(
        task_id=task.id,
        paperclip_issue_id=task.paperclip_issue_id or "",
        title=task.user_request,
        classification=classification,
        routing=routing,
        agent=agent,
        approved_plan=approved_plan,
        task_mode=task.task_mode,
        task_status=task.status,
    )

    # Output as JSON
    print(json.dumps(asdict(payload)))


if __name__ == "__main__":
    main()
