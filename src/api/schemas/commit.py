"""HTTP schemas for commit / replay Phase 1."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CommitRequest(BaseModel):
    # Tenant / agent / session are required for isolation and traceability.
    tenant_id: str
    agent_id: str
    session_id: str
    task_id: str
    trajectory: list[dict[str, Any]]
    labels: dict[str, Any] = Field(default_factory=dict)
    is_incremental: bool = False
    trajectory_id: str | None = None
    visualize_graph_png: bool = False


class CommitResponse(BaseModel):
    # Phase 1 returns graph size stats for observability.
    trajectory_id: str
    nodes: int
    edges: int
    status: str
    warnings: list[str] = Field(default_factory=list)


class ReplayResponse(BaseModel):
    # Replay mirrors persisted bundle fields used by debugging/UI.
    trajectory_id: str
    meta: dict[str, Any]
    trajectory: list[dict[str, Any]]
    graph_pointer: dict[str, Any]
    abstract: str
    overview: str
