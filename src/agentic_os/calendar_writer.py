"""
Google Calendar write operations for the agent inbox (franchieinc@gmail.com).

Only the agent account (gog.json / GOOGLE_CREDENTIALS_PATH) has write access.
Personal accounts are read-only and cannot be mutated here.

Public API
----------
create_event(title, start, end, ...)          → event dict with id
block_time(title, start, end, ...)            → same as create_event (no attendees)
update_event(event_id, ...)                   → updated event dict
delete_event(event_id)                        → True/False
add_reminder(event_id, minutes)              → updated event dict

Datetime format
---------------
start/end accept:
  • ISO 8601 with offset: "2026-03-24T10:00:00+01:00"
  • ISO 8601 UTC:          "2026-03-24T09:00:00Z"
  • Date only (all-day):   "2026-03-24"

When a timezone string is provided (e.g. "Europe/London") it is attached to
the event's start/end block so Google Calendar renders the time correctly in
the user's local zone.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

from .gmail_sender import _load_credentials, _refresh_access_token

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
_CREDS_ENV = "GOOGLE_CREDENTIALS_PATH"
_AGENT_CALENDAR = "primary"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_access_token(credentials_path: str | None = None) -> str:
    creds = _load_credentials(credentials_path or os.environ.get(_CREDS_ENV))
    return _refresh_access_token(
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        refresh_token=creds["refresh_token"],
    )


def _api_request(
    method: str,
    path: str,
    access_token: str,
    body: Optional[dict] = None,
) -> dict:
    """Execute a Calendar REST call; returns parsed JSON response."""
    url = f"{CALENDAR_API_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib_request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {access_token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib_request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return json.loads(raw.decode()) if raw.strip() else {}
    except urllib_error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        raise RuntimeError(f"Calendar API {method} {path} → HTTP {exc.code}: {body_text}") from exc


def _time_block(dt: str, timezone: str | None) -> dict:
    """Build a Calendar dateTime or date block from an ISO string."""
    if len(dt) == 10:  # "YYYY-MM-DD" → all-day
        return {"date": dt}
    block: dict[str, str] = {"dateTime": dt}
    if timezone:
        block["timeZone"] = timezone
    return block


def _build_event_body(
    title: str,
    start: str,
    end: str,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[list[str]] = None,
    reminders_minutes: Optional[list[int]] = None,
    timezone: Optional[str] = None,
) -> dict:
    body: dict[str, Any] = {
        "summary": title,
        "start": _time_block(start, timezone),
        "end": _time_block(end, timezone),
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": e} for e in attendees]
    if reminders_minutes is not None:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": m} for m in reminders_minutes
            ],
        }
    return body


def _normalise_response(raw: dict) -> dict:
    """Trim the Calendar API response to the fields we care about."""
    start_block = raw.get("start") or {}
    end_block = raw.get("end") or {}
    return {
        "event_id": raw.get("id"),
        "title": raw.get("summary"),
        "start": start_block.get("dateTime") or start_block.get("date"),
        "end": end_block.get("dateTime") or end_block.get("date"),
        "location": raw.get("location"),
        "description": raw.get("description"),
        "html_link": raw.get("htmlLink"),
        "status": raw.get("status"),
        "calendar_account": "franchieinc",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_event(
    title: str,
    start: str,
    end: str,
    *,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[list[str]] = None,
    reminders_minutes: Optional[list[int]] = None,
    timezone: Optional[str] = "Europe/London",
    credentials_path: Optional[str] = None,
) -> dict:
    """
    Create a new event on the agent calendar (franchieinc@gmail.com).

    Parameters
    ----------
    title             : Event title / summary
    start             : ISO 8601 start datetime or date string
    end               : ISO 8601 end datetime or date string
    description       : Optional event notes
    location          : Optional location string
    attendees         : Optional list of attendee email addresses
    reminders_minutes : Popup reminder times in minutes before event,
                        e.g. [10, 30] — overrides the calendar default
    timezone          : IANA timezone name (default "Europe/London")
    credentials_path  : Override default credentials file

    Returns event dict with event_id, title, start, end, html_link.
    Raises RuntimeError on API failure.
    """
    token = _get_access_token(credentials_path)
    body = _build_event_body(
        title=title,
        start=start,
        end=end,
        description=description,
        location=location,
        attendees=attendees,
        reminders_minutes=reminders_minutes,
        timezone=timezone,
    )
    raw = _api_request(
        "POST",
        f"/calendars/{_AGENT_CALENDAR}/events",
        token,
        body,
    )
    return _normalise_response(raw)


def block_time(
    title: str,
    start: str,
    end: str,
    *,
    description: Optional[str] = None,
    timezone: Optional[str] = "Europe/London",
    credentials_path: Optional[str] = None,
) -> dict:
    """
    Block time on the agent calendar — creates a private event with no
    attendees.  Useful for focus time, reminders, and task slots.

    Returns event dict with event_id, title, start, end, html_link.
    """
    return create_event(
        title=title,
        start=start,
        end=end,
        description=description,
        timezone=timezone,
        credentials_path=credentials_path,
    )


def update_event(
    event_id: str,
    *,
    title: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    reminders_minutes: Optional[list[int]] = None,
    timezone: Optional[str] = "Europe/London",
    credentials_path: Optional[str] = None,
) -> dict:
    """
    Patch an existing event on the agent calendar.

    Only fields that are not None are sent in the PATCH request, so you can
    update individual properties without overwriting the rest.

    Returns the updated event dict.
    Raises RuntimeError on API failure or if the event isn't found.
    """
    token = _get_access_token(credentials_path)
    patch: dict[str, Any] = {}
    if title is not None:
        patch["summary"] = title
    if start is not None:
        patch["start"] = _time_block(start, timezone)
    if end is not None:
        patch["end"] = _time_block(end, timezone)
    if description is not None:
        patch["description"] = description
    if location is not None:
        patch["location"] = location
    if reminders_minutes is not None:
        patch["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": m} for m in reminders_minutes],
        }
    if not patch:
        raise ValueError("No fields to update — provide at least one keyword argument.")
    raw = _api_request(
        "PATCH",
        f"/calendars/{_AGENT_CALENDAR}/events/{event_id}",
        token,
        patch,
    )
    return _normalise_response(raw)


def delete_event(
    event_id: str,
    *,
    credentials_path: Optional[str] = None,
) -> bool:
    """
    Delete an event from the agent calendar.

    Returns True on success, False on any error (logged to stderr).
    """
    try:
        token = _get_access_token(credentials_path)
        _api_request(
            "DELETE",
            f"/calendars/{_AGENT_CALENDAR}/events/{event_id}",
            token,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[calendar_writer] delete_event({event_id!r}) failed: {exc}", file=sys.stderr)
        return False


def add_reminder(
    event_id: str,
    minutes: int,
    *,
    credentials_path: Optional[str] = None,
) -> dict:
    """
    Add (or replace) a popup reminder on an existing event.

    Fetches the current event reminders, appends the new minute value
    (deduped), then PATCHes the event.

    Returns the updated event dict.
    """
    token = _get_access_token(credentials_path)
    # Fetch current event to read existing reminders
    raw = _api_request(
        "GET",
        f"/calendars/{_AGENT_CALENDAR}/events/{event_id}",
        token,
    )
    existing_reminders = raw.get("reminders", {})
    current_overrides = existing_reminders.get("overrides", [])
    existing_minutes = {r["minutes"] for r in current_overrides if r.get("method") == "popup"}
    existing_minutes.add(minutes)
    new_overrides = [{"method": "popup", "minutes": m} for m in sorted(existing_minutes)]
    patch = {"reminders": {"useDefault": False, "overrides": new_overrides}}
    updated = _api_request(
        "PATCH",
        f"/calendars/{_AGENT_CALENDAR}/events/{event_id}",
        token,
        patch,
    )
    return _normalise_response(updated)
