#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    src_path = repo_root / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))

    from agentic_os.cli import main as cli_main

    return cli_main(["notion", "sync-tasks", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
