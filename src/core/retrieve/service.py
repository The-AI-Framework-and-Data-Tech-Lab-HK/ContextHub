"""Retrieve service (Phase 2 semantic recall first)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.retrieve.candidate_union import union_candidates
from core.retrieve.evidence_builder import build_semantic_evidence
from core.retrieve.graph_recall import GraphMatch, recall_graph_candidates
from core.retrieve.query_parser import parse_retrieve_query
from core.retrieve.query_graph_builder import build_query_graph
from core.retrieve.reranker import rerank_semantic_only
from core.retrieve.semantic_recall import SemanticRecall


@dataclass
class RetrieveCommand:
    account_id: str
    agent_id: str
    query: dict[str, Any]
    scope_filter: list[str] | None = None
    owner_space_filter: list[str] | None = None
    top_k: int = 5
    include_full_clean_graph: bool = False


@dataclass
class RetrieveResult:
    items: list[dict[str, Any]]
    warnings: list[str]


class RetrieveService:
    WEIGHT_SEMANTIC = 0.45
    WEIGHT_GRAPH = 0.45

    def __init__(
        self,
        *,
        semantic_recall: SemanticRecall | None,
        clean_graph_loader: Any | None = None,
        query_dataflow_extractor: Any | None = None,
        query_temporal_fallback_edge: bool = True,
        query_reasoning_min_confidence: float = 0.55,
    ) -> None:
        self.semantic_recall = semantic_recall
        self.clean_graph_loader = clean_graph_loader
        self.query_dataflow_extractor = query_dataflow_extractor
        self.query_temporal_fallback_edge = query_temporal_fallback_edge
        self.query_reasoning_min_confidence = query_reasoning_min_confidence

    @staticmethod
    def _graph_evidence(match: GraphMatch) -> dict[str, Any]:
        return {
            "matched_nodes": list(match.matched_nodes),
            "matched_subgraph": (
                f"mcs_nodes={match.matched_mcs_nodes}, "
                f"mcs_edges={match.matched_mcs_edges}, "
                "node_rule=action_name_equal, edge_rule=edge_type_equal"
            ),
            "graph_match": {
                "matched_mcs_nodes": match.matched_mcs_nodes,
                "matched_mcs_edges": match.matched_mcs_edges,
                "query_nodes": match.query_nodes,
                "query_edges": match.query_edges,
                "node_match_rule": "action_name_equal",
                "edge_match_rule": "edge_type_equal",
            },
        }

    @classmethod
    def _combine_scores(cls, *, semantic_score: float, graph_match_score: float | None) -> float:
        if graph_match_score is None:
            return float(semantic_score)
        w_sem = float(cls.WEIGHT_SEMANTIC)
        w_graph = float(cls.WEIGHT_GRAPH)
        denom = max(1e-9, w_sem + w_graph)
        return (w_sem * float(semantic_score) + w_graph * float(graph_match_score)) / denom

    @staticmethod
    def _is_visible_to_agent(*, agent_id: str, scope: str, owner_space: str) -> bool:
        s = (scope or "agent").strip().lower() or "agent"
        o = str(owner_space or "").strip()
        if s == "datalake":
            return True
        if s in {"agent", "user"}:
            return o == agent_id
        if s == "team":
            # MVP rule: team resource is visible when requester belongs to that owner space.
            # Detailed team-closure expansion is delegated to ACL service integration later.
            return o == agent_id
        return False

    @classmethod
    def _acl_filter_visible(cls, *, items: list[dict[str, Any]], agent_id: str) -> list[dict[str, Any]]:
        visible: list[dict[str, Any]] = []
        for item in items:
            scope = str(item.get("scope") or "agent")
            owner_space = str(item.get("owner_space") or "")
            if cls._is_visible_to_agent(agent_id=agent_id, scope=scope, owner_space=owner_space):
                visible.append(item)
        return visible

    def run(self, cmd: RetrieveCommand) -> RetrieveResult:
        if self.semantic_recall is None:
            return RetrieveResult(items=[], warnings=["semantic recall backend is not configured"])

        pq = parse_retrieve_query(cmd.query)
        warnings: list[str] = []
        hits = self.semantic_recall.recall(
            account_id=cmd.account_id,
            agent_id=cmd.agent_id,
            query_text=pq.query_text,
            top_k=cmd.top_k,
            scope_filter=cmd.scope_filter,
            owner_space_filter=cmd.owner_space_filter,
            task_type=pq.task_type,
        )
        unioned = union_candidates(hits)
        ranked_semantic = rerank_semantic_only(unioned)

        graph_matches: dict[str, GraphMatch] = {}
        partial = cmd.query.get("partial_trajectory")
        partial_steps = partial if isinstance(partial, list) else None
        has_partial = bool(partial_steps)
        if has_partial and self.clean_graph_loader is not None:
            query_graph = build_query_graph(
                partial_trajectory=partial_steps or [],
                trajectory_id="query",
                dataflow_extractor=self.query_dataflow_extractor,
                temporal_fallback_edge=self.query_temporal_fallback_edge,
                reasoning_min_confidence=self.query_reasoning_min_confidence,
            )
            if query_graph is None:
                warnings.append("graph recall skipped: failed to build query graph from partial_trajectory")
            else:
                candidate_ids = [h.trajectory_id for h in ranked_semantic]
                graph_matches = recall_graph_candidates(
                    query_graph=query_graph,
                    candidate_trajectory_ids=candidate_ids,
                    clean_graph_loader=self.clean_graph_loader,
                )
        elif has_partial and self.clean_graph_loader is None:
            warnings.append("graph recall skipped: graph backend is not configured")

        items: list[dict[str, Any]] = []
        for hit in ranked_semantic:
            rationale = ["semantic recall match on trajectory-level summaries (L0/L1)"]
            if pq.task_type:
                rationale.append(f"task_type hint: {pq.task_type}")
            graph_match = graph_matches.get(hit.trajectory_id)
            graph_match_score = graph_match.graph_score if graph_match is not None else None
            if graph_match is not None:
                rationale.append("graph recall match via max common subgraph (MCS)")
                evidence = build_semantic_evidence(hit)
                evidence.update(self._graph_evidence(graph_match))
            else:
                if pq.has_partial_trajectory:
                    rationale.append("partial_trajectory detected; using semantic score fallback")
                evidence = build_semantic_evidence(hit)
            total_score = self._combine_scores(
                semantic_score=float(hit.semantic_score),
                graph_match_score=graph_match_score,
            )
            items.append(
                {
                    "trajectory_id": hit.trajectory_id,
                    "scope": str(
                        ((hit.raw_hits[0].get("metadata") or {}) if hit.raw_hits else {}).get("scope") or "agent"
                    ),
                    "owner_space": str(
                        ((hit.raw_hits[0].get("metadata") or {}) if hit.raw_hits else {}).get("owner_space")
                        or ((hit.raw_hits[0].get("metadata") or {}) if hit.raw_hits else {}).get("agent_id")
                        or ""
                    ),
                    "uri": (hit.matched_uris[0] if hit.matched_uris else None),
                    "score": total_score,
                    "total_score": total_score,
                    "semantic_score": hit.semantic_score,
                    "graph_match_score": graph_match_score,
                    "rationale": rationale,
                    "evidence": evidence,
                }
            )
        acl_before = len(items)
        items = self._acl_filter_visible(items=items, agent_id=cmd.agent_id)
        acl_after = len(items)
        if acl_after < acl_before:
            warnings.append(f"acl filtered {acl_before - acl_after} invisible candidates")
        items = sorted(items, key=lambda x: float(x.get("score") or 0.0), reverse=True)
        return RetrieveResult(items=items[: max(1, int(cmd.top_k))], warnings=warnings)
