"""
Safe SQLite backup using sqlite3.Connection.backup() — hot-backup safe.

Usage:
    python3 -m agentic_os.backup

Backs up:
  - agentic_os.sqlite3   (via sqlite3.Connection.backup — safe under concurrent writes)
  - audit_log*.jsonl     (file copy)
  - artifacts/           (directory copy)

Output: JSON summary to stdout.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path("/Users/dara/agents/agentic-os")
DB_SRC = BASE_DIR / "data" / "agentic_os.sqlite3"
BACKUP_ROOT = Path("/Users/dara/.openclaw/backups/agentic-os")
RETAIN_DAYS = 7


def _safe_backup_db(src: Path, dst: Path) -> None:
    """Use sqlite3.Connection.backup() for a transaction-safe hot copy."""
    with sqlite3.connect(str(src)) as src_conn:
        with sqlite3.connect(str(dst)) as dst_conn:
            src_conn.backup(dst_conn)


def _copy_audit_logs(src_data: Path, dest_data: Path) -> None:
    for f in src_data.glob("audit_log*"):
        shutil.copy2(f, dest_data / f.name)


def _copy_artifacts(src_artifacts: Path, dest_artifacts: Path) -> None:
    if src_artifacts.exists():
        shutil.copytree(src_artifacts, dest_artifacts, dirs_exist_ok=True)


def _prune_old_backups(backup_root: Path, retain: int) -> int:
    dated_dirs = sorted(
        d for d in backup_root.iterdir()
        if d.is_dir() and len(d.name) == 10 and d.name[4] == "-" and d.name[7] == "-"
    )
    deleted = 0
    for old in dated_dirs[:-retain] if retain > 0 else dated_dirs:
        shutil.rmtree(old)
        deleted += 1
    return deleted


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main() -> None:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest = BACKUP_ROOT / date_str
    dest_data = dest / "data"
    dest_artifacts = dest / "artifacts"
    dest_data.mkdir(parents=True, exist_ok=True)
    dest_artifacts.mkdir(parents=True, exist_ok=True)

    # 1. Safe SQLite hot backup
    dst_db = dest_data / "agentic_os.sqlite3"
    _safe_backup_db(DB_SRC, dst_db)

    # 2. Copy audit logs
    _copy_audit_logs(BASE_DIR / "data", dest_data)

    # 3. Copy artifacts directory
    _copy_artifacts(BASE_DIR / "artifacts", dest_artifacts)

    # 4. Prune old backups
    deleted = _prune_old_backups(BACKUP_ROOT, RETAIN_DAYS)

    # 5. Report
    size = _dir_size(dest)
    print(json.dumps({
        "status": "ok",
        "backup_path": str(dest),
        "deleted_count": deleted,
        "size_bytes": size,
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        sys.exit(1)
