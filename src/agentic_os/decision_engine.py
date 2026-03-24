"""
Decision engine — lightweight advisory layer using local Ollama phi4-mini:3.8b.

Allowed uses:
  - Ambiguous agent routing (main / manager / senior)
  - Clarification detection (is the brief too vague to execute?)
  - Retry classification (transient / permanent / needs_review)
  - Brief compression (extractive, preserves hard constraints)

NOT used for:
  - Approvals
  - Policy decisions
  - Final state transitions
  - Artifact acceptance
  - External-write authorisation

All functions apply deterministic rules first and call the model only when
the input is genuinely ambiguous. If Ollama is unavailable or returns invalid
JSON, every function falls back deterministically — the execution loop never
blocks on model availability.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "phi4-mini:3.8b"
TIMEOUT_S = 10

_VALID_AGENTS = ("main", "manager", "senior")
_VALID_RETRY_CLASSES = ("transient", "permanent", "needs_review")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _call(prompt: str, fallback: dict[str, Any]) -> dict[str, Any]:
    """
    Send a prompt to Ollama and parse the JSON response.
    Returns `fallback` on any error (network, timeout, non-JSON, missing keys).
    """
    payload = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
    }).encode()
    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = json.loads(resp.read())
            return json.loads(body["response"])
    except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError):
        return fallback
    except Exception:
        return fallback


# ── Public API ────────────────────────────────────────────────────────────────

def resolve_agent(
    domain: str,
    intent_type: str,
    risk_level: str,
    routing: str,
) -> str:
    """
    Resolve the best agent for a task.

    Deterministic fast-path handles the common cases. The model is called only
    when the inputs are genuinely ambiguous (i.e. no deterministic rule fires).

    Returns one of: "main", "manager", "senior".
    """
    # Deterministic rules — evaluated before any model call
    if risk_level == "high":
        return "senior"
    if intent_type in ("read", "capture") and risk_level == "low":
        return "main"
    if domain == "technical" and intent_type in ("execute",) and risk_level == "medium":
        return "manager"
    if intent_type == "content" and risk_level in ("low", "medium"):
        return "manager"
    if routing == "auto_execute" and risk_level == "low":
        return "main"
    if routing == "auto_execute" and risk_level == "medium":
        return "manager"

    # Ambiguous — ask the model
    prompt = (
        f"Choose one agent for this task.\n"
        f"domain={domain} intent={intent_type} risk={risk_level} routing={routing}\n"
        f"Agents: main=simple low-risk, manager=normal tasks, senior=complex/high-risk.\n"
        f'Reply ONLY with valid JSON: {{"agent": "main"|"manager"|"senior"}}'
    )
    result = _call(prompt, fallback={"agent": "manager"})
    agent = result.get("agent", "manager")
    return agent if agent in _VALID_AGENTS else "manager"


def detect_clarification(brief: str) -> dict[str, Any]:
    """
    Detect whether a brief is too vague to execute without asking the user.

    Returns: {"needs_clarification": bool, "reason": str | None}

    Deterministic guard: briefs shorter than 30 characters are always flagged.
    """
    if len(brief.strip()) < 30:
        return {"needs_clarification": True, "reason": "brief too short to act on"}

    prompt = (
        "Does this task brief contain enough information to execute without asking the user?\n"
        f"Brief (first 500 chars): {brief[:500]}\n"
        "Reply ONLY with valid JSON.\n"
        'If clear: {"needs_clarification": false, "reason": null}\n'
        'If unclear: {"needs_clarification": true, "reason": "one-sentence reason"}'
    )
    fallback: dict[str, Any] = {"needs_clarification": False, "reason": None}
    result = _call(prompt, fallback)
    return {
        "needs_clarification": bool(result.get("needs_clarification", False)),
        "reason": result.get("reason") or None,
    }


def classify_retry(error: str, domain: str, attempt_count: int) -> str:
    """
    Classify a task failure to determine the retry path.

    Returns one of: "transient", "permanent", "needs_review".

    Deterministic rules handle the obvious cases. The model is called when the
    error message does not match any known pattern.
    """
    # Always escalate after too many attempts
    if attempt_count >= 3:
        return "needs_review"

    error_lower = error.lower()

    # Transient: infrastructure / rate-limit errors
    if any(k in error_lower for k in (
        "timeout", "timed out", "rate limit", "429", "503", "502",
        "network", "connection", "unavailable",
    )):
        return "transient"

    # Permanent: parse / format / constraint errors
    if any(k in error_lower for k in (
        "missing result", "result_start", "parse error", "invalid",
        "not found", "permission denied", "spawn_failed",
    )):
        return "permanent"

    # Ambiguous — ask the model
    prompt = (
        f"Classify this task failure for retry routing.\n"
        f"error={error!r} domain={domain} attempts={attempt_count}\n"
        "transient=infrastructure/rate-limit, permanent=logic/parse error, needs_review=unclear.\n"
        'Reply ONLY with valid JSON: {"classification": "transient"|"permanent"|"needs_review"}'
    )
    result = _call(prompt, fallback={"classification": "needs_review"})
    c = result.get("classification", "needs_review")
    return c if c in _VALID_RETRY_CLASSES else "needs_review"


def compress_brief(brief: str, max_chars: int = 2000) -> str:
    """
    Extractively compress a brief that exceeds max_chars.

    Rules enforced:
    - Extractive only — original wording is preserved, filler is removed
    - Hard constraints MUST be preserved: task_id, intent, deadlines,
      output format markers (RESULT_START/RESULT_END/TASK_DONE), external refs
    - Falls back to simple truncation if model is unavailable or markers are lost

    Returns the compressed brief (or original if already within limit).
    """
    if len(brief) <= max_chars:
        return brief

    # Identify mandatory markers that must survive compression
    mandatory_markers = [m for m in ("RESULT_START", "RESULT_END", "TASK_DONE") if m in brief]

    prompt = (
        f"Compress this task brief to under {max_chars} characters.\n"
        "Rules:\n"
        "- Extractive only: keep original wording, remove filler and examples\n"
        "- MUST preserve: task_id, intent, deadlines, output format markers, "
        "external refs, RESULT_START/RESULT_END/TASK_DONE lines verbatim\n"
        f"Brief:\n{brief}\n"
        f'Reply ONLY with valid JSON: {{"compressed_brief": "..."}}'
    )
    fallback: dict[str, Any] = {"compressed_brief": brief[:max_chars]}
    result = _call(prompt, fallback)
    compressed: str = result.get("compressed_brief") or brief[:max_chars]

    # Safety: verify mandatory markers are still present
    for marker in mandatory_markers:
        if marker not in compressed:
            return brief[:max_chars]

    return compressed
