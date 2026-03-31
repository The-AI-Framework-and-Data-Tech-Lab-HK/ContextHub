"""Promote trajectory command/result models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PromoteCommand:
    account_id: str
    agent_id: str
    trajectory_id: str
    target_team: str
    reason: str | None = None


@dataclass
class PromoteResult:
    source_uri: str
    target_uri: str
    trajectory_id: str
    scope: str
    owner_space: str
    status: str = "promoted"
    vector_index_summary: dict[str, Any] | None = None
