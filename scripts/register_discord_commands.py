#!/usr/bin/env python3
"""
Register (or refresh) agentic-os slash commands on a Discord application.

Usage:
    python scripts/register_discord_commands.py

Reads these env vars (loaded from .env if present):
    DISCORD_BOT_TOKEN        — bot token, for Authorization header
    DISCORD_APPLICATION_ID   — application (bot) id
    DISCORD_GUILD_ID         — optional; if set, commands register to this
                               guild only (instant availability). If unset,
                               commands register globally (up to 1h rollout).

Commands registered:
    /panel      — open the interactive control panel
    /pending    — list pending approvals (with Approve/Deny buttons)
    /tasks      — list in-progress tasks
    /overdue    — list overdue (>48h) tasks
    /failures   — list recent failures
    /status     — show system health
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

# Load .env so the script can run standalone
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
from agentic_os.config import _load_env_file  # noqa: E402

_load_env_file(_REPO_ROOT)

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_USER_AGENT = "DiscordBot (https://github.com/agentic-os, 1.0)"

COMMANDS = [
    {
        "name": "panel",
        "description": "Open the agentic-os control panel",
        "type": 1,  # CHAT_INPUT
    },
    {
        "name": "pending",
        "description": "Show pending approvals with inline Approve/Deny",
        "type": 1,
    },
    {
        "name": "tasks",
        "description": "Show in-progress tasks",
        "type": 1,
    },
    {
        "name": "overdue",
        "description": "Show overdue (>48h) tasks",
        "type": 1,
    },
    {
        "name": "failures",
        "description": "Show recent failed tasks",
        "type": 1,
    },
    {
        "name": "status",
        "description": "Show agentic-os system health",
        "type": 1,
    },
]


def main() -> int:
    bot_token = os.environ.get("DISCORD_BOT_TOKEN")
    app_id = os.environ.get("DISCORD_APPLICATION_ID")
    guild_id = os.environ.get("DISCORD_GUILD_ID")

    if not bot_token:
        print("ERROR: DISCORD_BOT_TOKEN not set", file=sys.stderr)
        return 2
    if not app_id:
        print("ERROR: DISCORD_APPLICATION_ID not set", file=sys.stderr)
        return 2

    if guild_id:
        url = f"{DISCORD_API_BASE}/applications/{app_id}/guilds/{guild_id}/commands"
        scope = f"guild {guild_id}"
    else:
        url = f"{DISCORD_API_BASE}/applications/{app_id}/commands"
        scope = "global (up to 1h propagation)"

    print(f"Registering {len(COMMANDS)} commands to {scope}…")
    print(f"  URL: {url}")

    # PUT bulk-overwrites the command set (idempotent).
    req = urllib_request.Request(
        url,
        data=json.dumps(COMMANDS).encode(),
        headers={
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            "User-Agent": DISCORD_USER_AGENT,
        },
        method="PUT",
    )

    try:
        with urllib_request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib_error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(f"HTTP {exc.code}: {body}", file=sys.stderr)
        return 1
    except urllib_error.URLError as exc:
        print(f"network error: {exc}", file=sys.stderr)
        return 1

    print(f"Registered {len(data)} command(s):")
    for cmd in data:
        print(f"  /{cmd['name']}  —  {cmd.get('description', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
