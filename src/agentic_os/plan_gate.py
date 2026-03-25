"""
Plan gate — task mode classification for plan_first vs direct flows.

plan_first tasks require an operator-approved plan before execution starts.
direct tasks skip the planning stage entirely.

Classification rules (in priority order):
  1. medium/high risk → plan_first
  2. technical execute → plan_first
  3. content intent → plan_first
  4. everything else → direct
"""
from __future__ import annotations

# Risk levels that always require a plan gate.
# medium-risk tasks are already gated by the approval_required policy;
# plan_first is reserved for high-risk work that needs an upfront execution plan.
_PLAN_FIRST_RISK_LEVELS = frozenset({"high"})

# (domain, intent_type) pairs that require a plan gate at low risk
_PLAN_FIRST_LOW_RISK_PAIRS = frozenset({
    ("technical", "execute"),
})

# Intent types that always require a plan gate (regardless of domain)
_PLAN_FIRST_INTENTS = frozenset({"content"})


def classify_task_mode(domain: str, intent_type: str, risk_level: str) -> str:
    """
    Return 'plan_first' or 'direct' based on task characteristics.

    plan_first is used for:
    - higher-risk work (medium or high risk_level)
    - complex technical execution (technical + execute)
    - writing/proposal work (content intent)

    Everything else defaults to 'direct'.
    """
    if risk_level in _PLAN_FIRST_RISK_LEVELS:
        return "plan_first"
    if (domain, intent_type) in _PLAN_FIRST_LOW_RISK_PAIRS:
        return "plan_first"
    if intent_type in _PLAN_FIRST_INTENTS:
        return "plan_first"
    return "direct"
