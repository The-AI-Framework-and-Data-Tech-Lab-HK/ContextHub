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


class RetrieveRequest(BaseModel):
    # agent_id is kept for backward compatibility.
    # Preferred context source is headers: X-Account-Id / X-Agent-Id.
    agent_id: str | None = None
    query: RetrieveQuery
    scope: list[str] = Field(default_factory=list)
    owner_space: list[str] = Field(default_factory=list)
    top_k: int = Field(default=5, ge=1, le=100)
    include_full_clean_graph: bool = False


class RetrieveEvidence(BaseModel):
    matched_nodes: list[str] = Field(default_factory=list)
    matched_subgraph: str | None = None
    matched_uris: list[str] = Field(default_factory=list)
    graph_match: dict[str, Any] | None = None


class RetrieveItem(BaseModel):
    trajectory_id: str
    scope: str | None = None
    owner_space: str | None = None
    uri: str | None = None
    score: float
    total_score: float | None = None
    semantic_score: float
    graph_match_score: float | None = None
    rationale: list[str] = Field(default_factory=list)
    evidence: RetrieveEvidence = Field(default_factory=RetrieveEvidence)
    abstract: str | None = None
    overview: str | None = None
    clean_graph: dict[str, Any] | None = None


class RetrieveResponse(BaseModel):
    items: list[RetrieveItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
