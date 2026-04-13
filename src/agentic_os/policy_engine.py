"""
Policy engine — deterministic, label-driven execution gating.

Two questions:
  1. Does it need human approval?  (label: auto_execute bypasses; routines bypass;
     technical/system domains require it)
  2. Does it need planning?  (label: needs_plan)

Four possible verdicts:
  execute        — run immediately
  plan           — plan first, then execute
  approve        — wait for human approval, then execute
  approve_plan   — wait for human approval, then plan, then execute

The engine is agent-agnostic. Agent assignment is the caller's responsibility:
  - Backend-first tasks: caller supplies agent_key at creation time
  - Paperclip-first tasks: use existing assigneeAgentId, or fall back to
    domain-based default
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

POLICY_ACTIONS = ("execute", "plan", "approve", "approve_plan")


@dataclass(frozen=True)
class PolicyVerdict:
    action: str           # one of POLICY_ACTIONS
    needs_approval: bool
    needs_plan: bool

    def __post_init__(self) -> None:
        if self.action not in POLICY_ACTIONS:
            raise ValueError(f"invalid policy action: {self.action!r}")


def resolve(
    *,
    origin: str,
    domain: str,
    labels: Sequence[str] = (),
) -> PolicyVerdict:
    """
    Resolve execution policy for a task.

    Args:
        origin: where the task came from — "routine", "manual", "api", etc.
        domain: task domain — "technical", "system", "personal", "finance"
        labels: Paperclip labels attached to the issue (e.g. ["auto_execute", "needs_plan"])

    Returns:
        PolicyVerdict with the gating decision.
    """
    label_set = {l.lower().strip() for l in labels}

    auto_execute = "auto_execute" in label_set
    needs_plan = "needs_plan" in label_set

    # Routines and operator-override bypass approval
    if origin == "routine" or auto_execute:
        needs_approval = False
    else:
        # Technical/system domains need explicit approval (code/infra will be modified)
        needs_approval = domain in ("technical", "system")

    if needs_approval and needs_plan:
        action = "approve_plan"
    elif needs_approval:
        action = "approve"
    elif needs_plan:
        action = "plan"
    else:
        action = "execute"

    return PolicyVerdict(
        action=action,
        needs_approval=needs_approval,
        needs_plan=needs_plan,
    )


# ---------------------------------------------------------------------------
# Domain → default agent (used only for Paperclip-first tasks that have no
# assigneeAgentId set). Backend-first tasks must supply their own agent_key.
# ---------------------------------------------------------------------------

DOMAIN_DEFAULT_AGENT: dict[str, str] = {
    "technical": "engineer",
    "system": "infrastructure_engineer",
    "finance": "accountant",
    "personal": "executive_assistant",
}


def default_agent_for_domain(domain: str) -> str:
    """Fallback agent when Paperclip issue has no assignee."""
    return DOMAIN_DEFAULT_AGENT.get(domain, "project_manager")
