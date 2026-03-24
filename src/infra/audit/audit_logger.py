"""Append-only JSONL audit logger."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class JsonlAuditLogger:
    def __init__(self, file_path: str) -> None:
        self.path = Path(file_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, action: str, result: str, details: dict[str, Any]) -> None:
        # JSONL keeps append-only semantics and easy grep/parsing.
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "action": action,
            "result": result,
            "details": details,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
