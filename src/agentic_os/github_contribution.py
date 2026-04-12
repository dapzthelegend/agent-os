from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional


_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)

PR_STATUSES = frozenset({"open", "merged", "closed_unmerged", "none"})
LIFECYCLE_STATES = frozenset(
    {
        "issue_selected_work_started",
        "no_pr_started",
        "pr_open_under_review",
        "followup_required",
        "waiting_on_maintainer",
        "merged_ready_to_close",
        "closed_unmerged",
    }
)


@dataclass(frozen=True)
class GitHubContributionResult:
    payload: dict[str, Any]
    content_without_block: str


def _to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_payload(raw: dict[str, Any]) -> dict[str, Any]:
    repo = str(raw.get("repo") or "").strip() or None
    repo_name = str(raw.get("repo_name") or "").strip() or None
    if repo_name is None and repo and "/" in repo:
        repo_name = repo.split("/", 1)[1]

    pr_status = str(raw.get("pr_status") or "").strip() or "none"
    if pr_status not in PR_STATUSES:
        pr_status = "none"

    lifecycle_state = str(raw.get("lifecycle_state") or "").strip()
    if lifecycle_state not in LIFECYCLE_STATES:
        lifecycle_state = _infer_lifecycle_state(pr_status=pr_status, payload=raw)

    strategy_path = str(raw.get("repo_strategy_path") or "").strip() or None
    if strategy_path is None and repo_name:
        strategy_path = f"/Users/dara/agents/projects/technical/repo-strategy/{repo_name}.md"

    return {
        "repo": repo,
        "repo_name": repo_name,
        "repo_strategy_path": strategy_path,
        "linked_issue_number": _to_int(raw.get("linked_issue_number")),
        "linked_pr_number": _to_int(raw.get("linked_pr_number")),
        "linked_pr_url": str(raw.get("linked_pr_url") or "").strip() or None,
        "lifecycle_state": lifecycle_state,
        "pr_status": pr_status,
        "responsible_assignee": str(raw.get("responsible_assignee") or "").strip() or None,
        "last_followup_timestamp": str(raw.get("last_followup_timestamp") or "").strip() or None,
        "next_followup_hint": str(raw.get("next_followup_hint") or "").strip() or None,
        "resolution": str(raw.get("resolution") or "").strip() or None,
        "strategy_memory_updated": bool(raw.get("strategy_memory_updated", False)),
    }


def _infer_lifecycle_state(*, pr_status: str, payload: dict[str, Any]) -> str:
    if pr_status == "merged":
        return "merged_ready_to_close"
    if pr_status == "closed_unmerged":
        return "closed_unmerged"
    if pr_status == "open":
        if payload.get("next_followup_hint"):
            return "followup_required"
        return "pr_open_under_review"
    return "no_pr_started"


def parse_github_contribution_result(content: str) -> Optional[GitHubContributionResult]:
    for match in _JSON_BLOCK_RE.finditer(content or ""):
        block = match.group(1)
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        raw = payload.get("github_contribution")
        if not isinstance(raw, dict):
            continue
        normalized = _normalize_payload(raw)
        stripped = _JSON_BLOCK_RE.sub("", content, count=1).strip()
        return GitHubContributionResult(payload=normalized, content_without_block=stripped)
    return None
