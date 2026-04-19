from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class ArtifactRecord:
    id: str
    task_id: str
    artifact_type: str
    version: int
    path: str
    created_at: str
    content_preview: Optional[str]


class ArtifactStore:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir

    def ensure(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        task_id: str,
        artifact_type: str,
        content: Any,
        version: int = 1,
        *,
        extension: Optional[str] = None,
    ) -> ArtifactRecord:
        task_dir = self.base_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        artifact_id = f"art_{uuid4().hex[:12]}"
        if extension is None:
            extension = "json" if isinstance(content, (dict, list)) else "md"
        path = task_dir / f"{artifact_id}.v{version}.{extension}"
        serialized = self._serialize(content)
        path.write_text(serialized, encoding="utf-8")
        preview = serialized[:120].replace("\n", " ")
        return ArtifactRecord(
            id=artifact_id,
            task_id=task_id,
            artifact_type=artifact_type,
            version=version,
            path=str(path),
            created_at=utc_now(),
            content_preview=preview or None,
        )

    def read_text(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    @staticmethod
    def _serialize(content: Any) -> str:
        if isinstance(content, (dict, list)):
            return json.dumps(content, indent=2, sort_keys=True)
        return str(content)

    @staticmethod
    def to_payload(record: ArtifactRecord) -> dict[str, Any]:
        return asdict(record)
