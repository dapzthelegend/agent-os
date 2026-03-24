#!/usr/bin/env python3
"""
Re-authorize Google OAuth credentials to add Calendar scopes.

Runs a local OAuth flow (opens browser → catches redirect → saves new
refresh_token) for one or all credential files.

Scopes granted:
  agent  (gog.json / franchieinc@gmail.com)
      gmail.readonly  gmail.send  calendar        ← full calendar write
  dapz   (gog_dapz.json / dapzthelegend@gmail.com)
      gmail.readonly  calendar.readonly
  sola   (gog_sola.json / solaaremuoluwadara@gmail.com)
      gmail.readonly  calendar.readonly

Usage:
  python3 scripts/reauth_google.py --account agent
  python3 scripts/reauth_google.py --account dapz
  python3 scripts/reauth_google.py --account sola
  python3 scripts/reauth_google.py --all
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib import parse as urllib_parse
from urllib import request as urllib_request

# ---------------------------------------------------------------------------
# Account config
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent

ACCOUNTS: dict[str, dict] = {
    "agent": {
        "creds_file": _REPO_ROOT / "gog.json",
        "email": "franchieinc@gmail.com",
        "scopes": [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/calendar",
        ],
    },
    "dapz": {
        "creds_file": _REPO_ROOT / "gog_dapz.json",
        "email": "dapzthelegend@gmail.com",
        "scopes": [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
        ],
    },
    "sola": {
        "creds_file": _REPO_ROOT / "gog_sola.json",
        "email": "solaaremuoluwadara@gmail.com",
        "scopes": [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
        ],
    },
}

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_REDIRECT_PORT = 9876
_REDIRECT_URI = f"http://localhost:{_REDIRECT_PORT}/callback"


# ---------------------------------------------------------------------------
# Local callback server
# ---------------------------------------------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    auth_code: str | None = None
    error: str | None = None

    def do_GET(self):  # noqa: N802
        parsed = urllib_parse.urlparse(self.path)
        params = dict(urllib_parse.parse_qsl(parsed.query))
        if "code" in params:
            _CallbackHandler.auth_code = params["code"]
            self._respond("Authorization successful. You can close this tab.")
        elif "error" in params:
            _CallbackHandler.error = params["error"]
            self._respond(f"Authorization failed: {params['error']}")
        else:
            self._respond("Unexpected callback — no code or error.")

    def _respond(self, message: str) -> None:
        body = f"<html><body><p>{message}</p></body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence access logs
        pass


def _wait_for_code() -> str:
    """Start a one-shot HTTP server and wait until the OAuth callback arrives."""
    server = HTTPServer(("localhost", _REDIRECT_PORT), _CallbackHandler)
    # Serve exactly one request then stop
    server.handle_request()
    server.server_close()
    if _CallbackHandler.error:
        raise RuntimeError(f"OAuth error: {_CallbackHandler.error}")
    if not _CallbackHandler.auth_code:
        raise RuntimeError("No authorization code received.")
    code = _CallbackHandler.auth_code
    _CallbackHandler.auth_code = None
    _CallbackHandler.error = None
    return code


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def _build_auth_url(client_id: str, scopes: list[str]) -> str:
    params = urllib_parse.urlencode({
        "client_id": client_id,
        "redirect_uri": _REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",   # force refresh_token even if already granted
    })
    return f"{_AUTH_ENDPOINT}?{params}"


def _exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    payload = urllib_parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": _REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib_request.Request(_TOKEN_ENDPOINT, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib_request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _save_refresh_token(creds_file: Path, new_refresh_token: str) -> None:
    data = json.loads(creds_file.read_text(encoding="utf-8"))
    blob = data.get("web") or data
    blob["refresh_token"] = new_refresh_token
    if "web" in data:
        data["web"] = blob
    else:
        data = blob
    creds_file.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-account flow
# ---------------------------------------------------------------------------

def reauth_account(name: str) -> None:
    cfg = ACCOUNTS[name]
    creds_file: Path = cfg["creds_file"]
    email: str = cfg["email"]
    scopes: list[str] = cfg["scopes"]

    if not creds_file.exists():
        print(f"  [!] Credentials file not found: {creds_file}", file=sys.stderr)
        return

    data = json.loads(creds_file.read_text(encoding="utf-8"))
    blob = data.get("web") or data
    client_id = blob["client_id"]
    client_secret = blob["client_secret"]

    auth_url = _build_auth_url(client_id, scopes)

    print(f"\n{'='*60}")
    print(f"Account : {name}  ({email})")
    print(f"Scopes  : {', '.join(s.split('/')[-1] for s in scopes)}")
    print(f"{'='*60}")
    print(f"\nOpening browser for authorization…")
    print(f"If the browser doesn't open, visit:\n\n  {auth_url}\n")

    # Open browser in background after the server starts listening
    def _open():
        webbrowser.open(auth_url)

    t = threading.Timer(0.5, _open)
    t.start()

    try:
        code = _wait_for_code()
    finally:
        t.cancel()

    print("  ✓ Authorization code received — exchanging for tokens…")
    token_data = _exchange_code(client_id, client_secret, code)

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        print(
            f"  [!] No refresh_token in response: {token_data}",
            file=sys.stderr,
        )
        return

    _save_refresh_token(creds_file, refresh_token)
    print(f"  ✓ refresh_token saved to {creds_file.name}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-authorize Google OAuth credentials with Calendar scopes."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--account",
        choices=list(ACCOUNTS),
        help="Which account to re-authorize",
    )
    group.add_argument(
        "--all",
        dest="all_accounts",
        action="store_true",
        help="Re-authorize all three accounts sequentially",
    )
    args = parser.parse_args()

    accounts = list(ACCOUNTS) if args.all_accounts else [args.account]
    for name in accounts:
        reauth_account(name)

    print("\nDone. Run --poll-calendar to verify.")


if __name__ == "__main__":
    main()
