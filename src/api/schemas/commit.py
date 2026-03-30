"""HTTP schemas for commit / replay Phase 1."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CommitRequest(BaseModel):
    # tenant_id/agent_id are kept for backward compatibility.
    # Preferred context source is headers: X-Account-Id / X-Agent-Id.
    tenant_id: str | None = None
    agent_id: str | None = None
    account_id: str | None = None
    scope: str = "agent"
    owner_space: str | None = None
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
    idempotency_key: str
    nodes: int
    edges: int
    status: str
    warnings: list[str] = Field(default_factory=list)
    summary_l0: str
    summary_l1: str
    neo4j_summary: dict[str, Any] = Field(default_factory=dict)
    vector_index_summary: dict[str, Any] = Field(default_factory=dict)


class ReplayResponse(BaseModel):
    # Replay mirrors persisted bundle fields used by debugging/UI.
    trajectory_id: str
    meta: dict[str, Any]
    trajectory: list[dict[str, Any]]
    graph_pointer: dict[str, Any]
    abstract: str
    overview: str
