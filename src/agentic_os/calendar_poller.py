"""
Google Calendar API poller — polls all three Google accounts.

Account matrix:
  franchieinc@gmail.com    (gog.json / GOOGLE_CREDENTIALS_PATH)
      → full calendar scope; agent's own calendar (create/read)
  dapzthelegend@gmail.com  (gog_dapz.json / GOOGLE_CREDENTIALS_PATH_PERSONAL_1)
      → calendar.readonly; personal calendar, read-only
  solaaremuoluwadara@gmail.com (gog_sola.json / GOOGLE_CREDENTIALS_PATH_PERSONAL_2)
      → calendar.readonly; personal calendar, read-only

Returns a merged CalendarSummary-compatible dict with events tagged by
`calendar_account` so the daily recap knows which calendar each event
belongs to.

On any per-account error the account is skipped with a stderr warning —
the remaining accounts still contribute.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from .gmail_sender import _load_credentials, _refresh_access_token

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"

# Env-var names for the three credential files
_CREDS_AGENT = "GOOGLE_CREDENTIALS_PATH"
_CREDS_PERSONAL_1 = "GOOGLE_CREDENTIALS_PATH_PERSONAL_1"
_CREDS_PERSONAL_2 = "GOOGLE_CREDENTIALS_PATH_PERSONAL_2"

_EMPTY_SUMMARY: dict = {"events": [], "constraints": []}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_events(
    access_token: str,
    time_min: str,
    time_max: str,
    max_results: int = 20,
) -> list[dict]:
    """Hit the Calendar API and return raw event items."""
    url = (
        f"{CALENDAR_API_BASE}/calendars/primary/events"
        f"?timeMin={urllib_parse.quote(time_min)}"
        f"&timeMax={urllib_parse.quote(time_max)}"
        f"&singleEvents=true&orderBy=startTime&maxResults={max_results}"
    )
    req = urllib_request.Request(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib_request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    return data.get("items", [])


def _normalise_events(raw_items: list[dict], account_label: str) -> list[dict]:
    """Convert raw Google Calendar items → CalendarEvent-compatible dicts."""
    events = []
    for item in raw_items:
        start_block = item.get("start") or {}
        end_block = item.get("end") or {}
        start = start_block.get("dateTime") or start_block.get("date")
        end = end_block.get("dateTime") or end_block.get("date")
        events.append({
            "title": item.get("summary") or "Untitled event",
            "start": start,
            "end": end,
            "location": item.get("location"),
            "summary": item.get("description"),
            "actionable": False,
            "calendar_account": account_label,
            "event_id": item.get("id"),
        })
    return events


def _poll_single_account(
    credentials_path: str | None,
    account_label: str,
    time_min: str,
    time_max: str,
) -> list[dict]:
    """
    Poll one account's primary calendar.  Returns [] on any error.
    """
    if not credentials_path:
        return []
    try:
        creds = _load_credentials(credentials_path)
        access_token = _refresh_access_token(
            client_id=creds["client_id"],
            client_secret=creds["client_secret"],
            refresh_token=creds["refresh_token"],
        )
        raw = _fetch_events(access_token, time_min, time_max)
        return _normalise_events(raw, account_label)
    except urllib_error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        if exc.code == 403:
            print(
                f"[calendar_poller] {account_label}: 403 — token may lack calendar scope. "
                "Run scripts/reauth_google.py to re-authorize.",
                file=sys.stderr,
            )
        else:
            print(
                f"[calendar_poller] {account_label}: HTTP {exc.code} — {body}",
                file=sys.stderr,
            )
        return []
    except Exception as exc:  # noqa: BLE001
        print(f"[calendar_poller] {account_label}: {exc}", file=sys.stderr)
        return []


def _sort_key(event: dict) -> str:
    """Sort key: events without a start time sort to the end."""
    return event.get("start") or "9999"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def poll_calendar(
    agent_credentials_path: str | None = None,
    personal1_credentials_path: str | None = None,
    personal2_credentials_path: str | None = None,
) -> dict:
    """
    Fetch today's events from all three Google Calendar accounts and return
    a merged CalendarSummary-compatible dict.

    Credential paths default to env vars:
      GOOGLE_CREDENTIALS_PATH           → franchieinc (agent / full write)
      GOOGLE_CREDENTIALS_PATH_PERSONAL_1 → dapzthelegend (read-only)
      GOOGLE_CREDENTIALS_PATH_PERSONAL_2 → solaaremuoluwadara (read-only)

    Each event in the returned list includes:
      title, start, end, location, summary, actionable,
      calendar_account, event_id

    Returns {"events": [...], "constraints": []} — never raises.
    """
    now_utc = datetime.now(timezone.utc)
    time_min = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    time_max = now_utc.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

    agent_path = agent_credentials_path or os.environ.get(_CREDS_AGENT)
    p1_path = personal1_credentials_path or os.environ.get(_CREDS_PERSONAL_1)
    p2_path = personal2_credentials_path or os.environ.get(_CREDS_PERSONAL_2)

    all_events: list[dict] = []

    all_events.extend(
        _poll_single_account(agent_path, "franchieinc", time_min, time_max)
    )
    all_events.extend(
        _poll_single_account(p1_path, "dapz", time_min, time_max)
    )
    all_events.extend(
        _poll_single_account(p2_path, "sola", time_min, time_max)
    )

    all_events.sort(key=_sort_key)

    return {"events": all_events, "constraints": []}
