from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Paths:
    root: Path
    data_dir: Path
    artifacts_dir: Path
    db_path: Path
    audit_log_path: Path
    policy_rules_path: Path

    @classmethod
    def from_root(cls, root: Path) -> "Paths":
        data_dir = root / "data"
        artifacts_dir = root / "artifacts"
        return cls(
            root=root,
            data_dir=data_dir,
            artifacts_dir=artifacts_dir,
            db_path=data_dir / "agentic_os.sqlite3",
            audit_log_path=data_dir / "audit_log.jsonl",
            policy_rules_path=root / "policy_rules.json",
        )


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_paths() -> Paths:
    return Paths.from_root(repo_root())


def load_policy_rules(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rules = payload.get("rules")
    if not isinstance(rules, list):
        raise ValueError(f"policy rules file must contain a top-level 'rules' list: {path}")
    return rules
