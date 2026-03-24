"""
Gmail sender via Gmail REST API + OAuth2 refresh-token flow.

Credentials are read from a JSON file whose path is given by the
GOOGLE_CREDENTIALS_PATH environment variable (falls back to gog.json
alongside the repo root).  The file must contain a top-level "web" key
with at least: client_id, client_secret, refresh_token.

No third-party libraries required — uses urllib from the standard library.
"""
from __future__ import annotations

import base64
import json
import os
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Optional
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

_CREDENTIALS_ENV = "GOOGLE_CREDENTIALS_PATH"
_DEFAULT_CREDS_NAME = "gog.json"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_email(
    to: str,
    subject: str,
    body: str,
    *,
    html_body: Optional[str] = None,
    sender: str = "franchieinc@gmail.com",
    credentials_path: Optional[str] = None,
) -> bool:
    """
    Send an email via the Gmail API.

    If html_body is provided, sends a multipart/alternative message with both
    plain-text and HTML parts. Otherwise sends plain-text only.

    Returns True on success, False on any error (logged to stderr).
    """
    try:
        creds = _load_credentials(credentials_path)
        access_token = _refresh_access_token(
            client_id=creds["client_id"],
            client_secret=creds["client_secret"],
            refresh_token=creds["refresh_token"],
        )
        raw = _build_raw_message(to=to, sender=sender, subject=subject, body=body, html_body=html_body)
        _send_raw(raw, access_token)
        return True
    except Exception as exc:  # noqa: BLE001
        import sys
        print(f"[gmail_sender] Failed to send email to {to!r}: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_credentials(credentials_path: Optional[str]) -> dict:
    path_str = credentials_path or os.environ.get(_CREDENTIALS_ENV)
    if not path_str:
        # default: gog.json at repo root (two levels above this file)
        path_str = str(Path(__file__).resolve().parents[3] / _DEFAULT_CREDS_NAME)
    data = json.loads(Path(path_str).read_text(encoding="utf-8"))
    creds = data.get("web") or data
    required = ("client_id", "client_secret", "refresh_token")
    missing = [k for k in required if not creds.get(k)]
    if missing:
        raise ValueError(f"Credentials file missing keys: {missing}")
    return creds


def _refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    payload = urllib_parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }).encode()
    req = urllib_request.Request(TOKEN_URL, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib_request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    access_token = data.get("access_token")
    if not access_token:
        raise RuntimeError(f"Token refresh failed: {data}")
    return access_token


def _build_raw_message(
    to: str,
    sender: str,
    subject: str,
    body: str,
    *,
    html_body: Optional[str] = None,
) -> str:
    if html_body:
        from email.mime.multipart import MIMEMultipart
        msg: Any = MIMEMultipart("alternative")
        msg["To"] = to
        msg["From"] = sender
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")
        msg["To"] = to
        msg["From"] = sender
        msg["Subject"] = subject
    raw_bytes = msg.as_bytes()
    return base64.urlsafe_b64encode(raw_bytes).decode()


def _send_raw(raw: str, access_token: str) -> None:
    payload = json.dumps({"raw": raw}).encode()
    req = urllib_request.Request(GMAIL_SEND_URL, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib_request.urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 204):
                body = resp.read().decode(errors="replace")
                raise RuntimeError(f"Gmail API returned {resp.status}: {body}")
    except urllib_error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"Gmail API HTTP {exc.code}: {body}") from exc
