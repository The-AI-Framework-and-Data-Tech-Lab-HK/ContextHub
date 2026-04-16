"""HTTP schemas for AMC promote endpoint."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class PromoteRequest(BaseModel):
    trajectory_id: str
    target_team: str
    reason: str | None = None


class PromoteResponse(BaseModel):
    source_uri: str
    target_uri: str
    trajectory_id: str
    scope: str
    owner_space: str
    status: str = "promoted"
    vector_index_summary: dict[str, Any] | None = None
