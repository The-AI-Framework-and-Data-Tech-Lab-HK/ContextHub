"""Idempotency helpers for commit (account + task + trajectory content hash)."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def trajectory_content_hash(trajectory: list[dict[str, Any]]) -> str:
    """Deterministic SHA-256 over canonical JSON of the raw trajectory list."""
    payload = json.dumps(trajectory, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def commit_idempotency_key(account_id: str, task_id: str, trajectory: list[dict[str, Any]]) -> str:
    """Key used to detect duplicate commits: account + task + content hash."""
    h = trajectory_content_hash(trajectory)
    return f"{account_id}:{task_id}:{h}"
