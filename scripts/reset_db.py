#!/usr/bin/env python3
"""
Reset the agentic-os task database.

Deletes the existing SQLite file and recreates it from the canonical schema.
Required after a breaking schema change (e.g. phase-0 migration).

Usage:
    python scripts/reset_db.py [--yes]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Resolve project root and add src to path
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from agentic_os.config import default_paths, load_app_config
from agentic_os.storage import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset the agentic-os task database.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt.",
    )
    args = parser.parse_args()

    paths = default_paths()
    db_path = paths.db_path

    if db_path.exists():
        if not args.yes:
            answer = input(f"Delete and recreate {db_path}? [y/N] ").strip().lower()
            if answer != "y":
                print("Aborted.")
                sys.exit(0)
        db_path.unlink()
        print(f"Deleted: {db_path}")
    else:
        print(f"Database not found, will create fresh: {db_path}")

    db = Database(db_path)
    db.initialize()
    print(f"Database recreated: {db_path}")

    # Validate paperclip config if present
    try:
        app_config = load_app_config(paths)
        if app_config.paperclip is not None:
            print(
                f"Paperclip config loaded — company_id={app_config.paperclip.company_id}"
            )
        else:
            print("No paperclip config found in agentic_os.config.json.")
    except Exception as exc:
        print(f"Warning: config validation error: {exc}", file=sys.stderr)

    print("Done.")


if __name__ == "__main__":
    main()
