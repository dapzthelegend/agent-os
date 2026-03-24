"""Parse approval replies from Dara's email responses."""
from __future__ import annotations

import re
from typing import Literal


def parse_approval_reply(email_body: str) -> Literal["approved", "rejected", "unclear"]:
    """
    Parse Dara's reply to an approval request email.

    The approval email instructs:
        To approve: reply "approve <task_id>"
        To reject:  reply "reject <task_id> <reason>"

    Matching is case-insensitive. Returns:
        "approved"  — body contains approve/approved
        "rejected"  — body contains reject/rejected/deny/denied
        "unclear"   — none of the above matched
    """
    body = email_body.strip().lower()

    if re.search(r"\bapprove[d]?\b|\blooks?\s+good\b|\bgo\s+ahead\b|\byes\b|\byep\b|\byup\b", body):
        return "approved"
    if re.search(r"\breject(?:ed)?\b|\bdeny\b|\bdenied\b|\bno\b|\bnope\b", body):
        return "rejected"

    return "unclear"
