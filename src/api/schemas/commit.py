"""HTTP schemas for commit / replay Phase 1."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CommitRequest(BaseModel):
    # agent_id is kept for backward compatibility.
    # Preferred context source is headers: X-Account-Id / X-Agent-Id.
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


class BatchCommitOptions(BaseModel):
    # Phase A keeps options mostly declarative; fail_fast is enforced.
    fail_fast: bool = False
    llm_batch_size_hint: int = 8
    llm_max_items_per_batch: int = 16
    llm_token_usage_ratio: float = 0.6
    persist_batch_size: int = 32


class BatchCommitItemRequest(BaseModel):
    session_id: str
    task_id: str
    trajectory: list[dict[str, Any]]
    labels: dict[str, Any] = Field(default_factory=dict)
    is_incremental: bool = False
    trajectory_id: str | None = None
    visualize_graph_png: bool = False


class BatchCommitRequest(BaseModel):
    # account/agent fields are kept for backward compatibility.
    batch_id: str | None = None
    agent_id: str | None = None
    account_id: str | None = None
    scope: str = "agent"
    owner_space: str | None = None
    options: BatchCommitOptions = Field(default_factory=BatchCommitOptions)
    items: list[BatchCommitItemRequest] = Field(default_factory=list)


class BatchCommitSummary(BaseModel):
    total: int
    accepted: int
    idempotent: int
    failed: int
    skipped: int = 0


class BatchCommitItemResponse(BaseModel):
    item_id: str
    trajectory_id: str | None = None
    idempotency_key: str | None = None
    status: str
    nodes: int | None = None
    edges: int | None = None
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    neo4j_summary: dict[str, Any] = Field(default_factory=dict)
    vector_index_summary: dict[str, Any] = Field(default_factory=dict)


class BatchCommitResponse(BaseModel):
    batch_id: str
    status: str
    summary: BatchCommitSummary
    items: list[BatchCommitItemResponse] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ReplayResponse(BaseModel):
    # Replay mirrors persisted bundle fields used by debugging/UI.
    trajectory_id: str
    meta: dict[str, Any]
    trajectory: list[dict[str, Any]]
    graph_pointer: dict[str, Any]
    abstract: str
    overview: str
