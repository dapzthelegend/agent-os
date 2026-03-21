#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_json_object(*, raw_value: str | None, file_path: str | None) -> dict[str, Any]:
    if raw_value and file_path:
        raise SystemExit("provide either --input-file or --input-json, not both")
    if raw_value:
        value = json.loads(raw_value)
    elif file_path:
        value = json.loads(Path(file_path).read_text(encoding="utf-8"))
    else:
        raise SystemExit("provide --input-file or --input-json")
    if not isinstance(value, dict):
        raise SystemExit("input must decode to a JSON object")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize OpenClaw daily payloads and run agentic-os daily routine",
    )
    parser.add_argument("--input-file")
    parser.add_argument("--input-json")
    parser.add_argument("--date")
    parser.add_argument("--timezone")
    parser.add_argument("--recipient")
    parser.add_argument("--delivery-time")
    parser.add_argument("--no-notion", action="store_true")
    parser.add_argument("--print-normalized", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    repo_root = _repo_root()
    src_path = repo_root / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))

    from agentic_os.cli import print_json
    from agentic_os.config import default_paths
    from agentic_os.openclaw_bridge import normalize_openclaw_daily_routine_payload
    from agentic_os.service import AgenticOSService

    parser = _build_parser()
    args = parser.parse_args(argv)

    raw_payload = _load_json_object(raw_value=args.input_json, file_path=args.input_file)
    normalized_payload = normalize_openclaw_daily_routine_payload(raw_payload)
    for key, value in (
        ("date", args.date),
        ("timezone", args.timezone),
        ("recipient", args.recipient),
        ("delivery_time", args.delivery_time),
    ):
        if value is not None:
            normalized_payload[key] = value

    if args.dry_run:
        print_json({"normalized_payload": normalized_payload})
        return 0

    service = AgenticOSService(default_paths())
    service.initialize()
    result = service.run_daily_routine(
        payload=normalized_payload,
        create_notion_tasks=not args.no_notion,
    )
    if args.print_normalized:
        print_json({"normalized_payload": normalized_payload, **result})
    else:
        print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
