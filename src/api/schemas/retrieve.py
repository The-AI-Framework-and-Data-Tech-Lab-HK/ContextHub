"""HTTP schemas for retrieve Phase 2 (semantic recall first)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RetrieveQueryConstraints(BaseModel):
    tool_whitelist: list[str] = Field(default_factory=list)


class RetrieveQuery(BaseModel):
    task_description: str = ""
    partial_trajectory: list[dict[str, Any]] | None = None
    constraints: RetrieveQueryConstraints = Field(default_factory=RetrieveQueryConstraints)
    task_type: str | None = None


class RetrieveRequest(BaseModel):
    tenant_id: str
    agent_id: str
    query: RetrieveQuery
    top_k: int = Field(default=5, ge=1, le=100)


class RetrieveEvidence(BaseModel):
    matched_nodes: list[str] = Field(default_factory=list)
    matched_subgraph: str | None = None
    matched_uris: list[str] = Field(default_factory=list)


class RetrieveItem(BaseModel):
    trajectory_id: str
    score: float
    semantic_score: float
    graph_score: float | None = None
    rationale: list[str] = Field(default_factory=list)
    evidence: RetrieveEvidence = Field(default_factory=RetrieveEvidence)
    abstract: str | None = None
    overview: str | None = None


class RetrieveResponse(BaseModel):
    items: list[RetrieveItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
