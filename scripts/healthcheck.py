#!/usr/bin/env python3
"""Health check script for agentic-os backend and related services."""

import sys
import subprocess
from pathlib import Path

# Ensure we can import agentic_os
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def check_sqlite() -> tuple[bool, str]:
    """Check SQLite database."""
    try:
        from agentic_os.storage import Database
        from agentic_os.config import default_paths
        
        paths = default_paths()
        db = Database(paths.db_path)
        db.initialize()
        return True, "DB ok"
    except Exception as exc:
        return False, str(exc)


def check_notion_api() -> tuple[bool, str]:
    """Check Notion API connectivity."""
    try:
        from agentic_os.notion import NotionAdapter
        from agentic_os.config import load_app_config, default_paths
        
        paths = default_paths()
        config = load_app_config(paths)
        
        if config.notion is None:
            return False, "Notion not configured"
        
        adapter = NotionAdapter(config.notion)
        adapter.query_tasks(limit=1)
        return True, "Notion API ok"
    except Exception as exc:
        return False, str(exc)


def check_cron() -> tuple[bool, str]:
    """Check if daily routine cron is configured."""
    try:
        result = subprocess.run(
            ["openclaw", "cron", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = result.stdout + result.stderr
        cron_id = "3a6d3126-a55c-4126-b028-7755741b5fa6"
        if "daily" in output and cron_id in output:
            return True, "Cron ok"
        return False, f"Cron not found (searched for {cron_id})"
    except Exception as exc:
        return False, str(exc)


def check_bridge_scripts() -> tuple[bool, str]:
    """Check that bridge scripts are importable."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import openclaw_daily_routine_bridge  # noqa: F401
        return True, "Bridge scripts ok"
    except ImportError as exc:
        return False, f"Cannot import bridge: {exc}"
    except Exception as exc:
        return False, str(exc)


def main() -> int:
    """Run all checks and report status."""
    checks = [
        ("SQLite", check_sqlite),
        ("Notion API", check_notion_api),
        ("Cron", check_cron),
        ("Bridge scripts", check_bridge_scripts),
    ]
    
    failed = 0
    for name, check_fn in checks:
        try:
            ok, msg = check_fn()
            status = "[OK]" if ok else "[FAIL]"
            print(f"{status} {name}: {msg}")
            if not ok:
                failed += 1
        except Exception as exc:
            print(f"[FAIL] {name}: {exc}")
            failed += 1
    
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
