"""Retrieve service (Phase 2 semantic recall first)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.retrieve.candidate_union import union_candidates
from core.retrieve.evidence_builder import build_semantic_evidence
from core.retrieve.query_parser import parse_retrieve_query
from core.retrieve.reranker import rerank_semantic_only
from core.retrieve.semantic_recall import SemanticRecall


@dataclass
class RetrieveCommand:
    tenant_id: str
    agent_id: str
    query: dict[str, Any]
    top_k: int = 5


@dataclass
class RetrieveResult:
    items: list[dict[str, Any]]
    warnings: list[str]


class RetrieveService:
    def __init__(self, *, semantic_recall: SemanticRecall | None) -> None:
        self.semantic_recall = semantic_recall

    def run(self, cmd: RetrieveCommand) -> RetrieveResult:
        if self.semantic_recall is None:
            return RetrieveResult(items=[], warnings=["semantic recall backend is not configured"])

        pq = parse_retrieve_query(cmd.query)
        hits = self.semantic_recall.recall(
            tenant_id=cmd.tenant_id,
            agent_id=cmd.agent_id,
            query_text=pq.query_text,
            top_k=cmd.top_k,
        )
        unioned = union_candidates(hits)
        ranked = rerank_semantic_only(unioned)

        items: list[dict[str, Any]] = []
        for hit in ranked[: max(1, int(cmd.top_k))]:
            rationale = ["semantic recall match on trajectory-level summaries (L0/L1)"]
            if pq.task_type:
                rationale.append(f"task_type hint: {pq.task_type}")
            if pq.has_partial_trajectory:
                rationale.append("partial_trajectory detected; graph recall is not enabled yet")
            items.append(
                {
                    "trajectory_id": hit.trajectory_id,
                    "score": hit.semantic_score,
                    "semantic_score": hit.semantic_score,
                    "graph_score": None,
                    "rationale": rationale,
                    "evidence": build_semantic_evidence(hit),
                }
            )
        return RetrieveResult(items=items, warnings=[])
