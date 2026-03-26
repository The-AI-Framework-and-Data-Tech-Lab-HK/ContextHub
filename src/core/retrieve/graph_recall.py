"""Graph recall with MCS-like matching for partial trajectories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class GraphMatch:
    trajectory_id: str
    graph_score: float
    matched_mcs_nodes: int
    matched_mcs_edges: int
    query_nodes: int
    query_edges: int
    matched_nodes: list[str]


def _node_label(node: dict[str, Any]) -> str:
    return str(node.get("tool_name") or "").strip().lower()


def _edge_type(edge: dict[str, Any]) -> str:
    return str(edge.get("dep_type") or "").strip().lower()


def _normalized_graph(graph: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(graph, dict):
        return [], []
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]
    return nodes, edges


def _best_mapping_backtracking(
    query_nodes: list[dict[str, Any]],
    query_edges: list[tuple[str, str, str]],
    cand_nodes: list[dict[str, Any]],
    cand_edge_set: set[tuple[str, str, str]],
) -> tuple[dict[str, str], int, int]:
    q_ids = [str(n.get("node_id") or "") for n in query_nodes if str(n.get("node_id") or "")]
    q_by_id = {str(n.get("node_id") or ""): n for n in query_nodes}
    c_ids = [str(n.get("node_id") or "") for n in cand_nodes if str(n.get("node_id") or "")]
    c_by_id = {str(n.get("node_id") or ""): n for n in cand_nodes}

    candidates_by_q: dict[str, list[str]] = {}
    for qid in q_ids:
        qlabel = _node_label(q_by_id[qid])
        matches = [cid for cid in c_ids if _node_label(c_by_id[cid]) == qlabel]
        candidates_by_q[qid] = matches

    ordered_qids = sorted(
        q_ids,
        key=lambda qid: (len(candidates_by_q.get(qid) or []), -sum(1 for s, d, _ in query_edges if s == qid or d == qid)),
    )

    best_map: dict[str, str] = {}
    best_nodes = 0
    best_edges = 0
    best_total = 0

    def _count_edges(mapping: dict[str, str]) -> int:
        hit = 0
        for src, dst, dep in query_edges:
            csrc = mapping.get(src)
            cdst = mapping.get(dst)
            if csrc and cdst and (csrc, cdst, dep) in cand_edge_set:
                hit += 1
        return hit

    def dfs(idx: int, mapping: dict[str, str], used_c: set[str]) -> None:
        nonlocal best_map, best_nodes, best_edges, best_total
        remaining = len(ordered_qids) - idx
        # Loose upper bound for pruning.
        possible_total = len(mapping) + remaining + len(query_edges)
        if possible_total <= best_total:
            return
        if idx >= len(ordered_qids):
            nodes_hit = len(mapping)
            edges_hit = _count_edges(mapping)
            total = nodes_hit + edges_hit
            if total > best_total or (total == best_total and edges_hit > best_edges):
                best_total = total
                best_nodes = nodes_hit
                best_edges = edges_hit
                best_map = dict(mapping)
            return

        qid = ordered_qids[idx]
        # Option A: skip this query node in common subgraph.
        dfs(idx + 1, mapping, used_c)
        # Option B: map this query node to one candidate node with same action/tool name.
        for cid in candidates_by_q.get(qid) or []:
            if cid in used_c:
                continue
            mapping[qid] = cid
            used_c.add(cid)
            dfs(idx + 1, mapping, used_c)
            used_c.remove(cid)
            mapping.pop(qid, None)

    dfs(0, {}, set())
    return best_map, best_nodes, best_edges


def _mcs_match(query_graph: dict[str, Any], candidate_graph: dict[str, Any], *, trajectory_id: str) -> GraphMatch:
    query_nodes, query_edges_raw = _normalized_graph(query_graph)
    cand_nodes, cand_edges_raw = _normalized_graph(candidate_graph)

    query_edges: list[tuple[str, str, str]] = []
    for e in query_edges_raw:
        src = str(e.get("src") or "")
        dst = str(e.get("dst") or "")
        dep = _edge_type(e)
        if src and dst and dep:
            query_edges.append((src, dst, dep))
    cand_edge_set: set[tuple[str, str, str]] = set()
    for e in cand_edges_raw:
        src = str(e.get("src") or "")
        dst = str(e.get("dst") or "")
        dep = _edge_type(e)
        if src and dst and dep:
            cand_edge_set.add((src, dst, dep))

    if not query_nodes:
        return GraphMatch(
            trajectory_id=trajectory_id,
            graph_score=0.0,
            matched_mcs_nodes=0,
            matched_mcs_edges=0,
            query_nodes=0,
            query_edges=0,
            matched_nodes=[],
        )

    node_map, matched_nodes, matched_edges = _best_mapping_backtracking(
        query_nodes=query_nodes,
        query_edges=query_edges,
        cand_nodes=cand_nodes,
        cand_edge_set=cand_edge_set,
    )
    denom = len(query_nodes) + len(query_edges)
    score = float(matched_nodes + matched_edges) / float(max(1, denom))
    return GraphMatch(
        trajectory_id=trajectory_id,
        graph_score=score,
        matched_mcs_nodes=matched_nodes,
        matched_mcs_edges=matched_edges,
        query_nodes=len(query_nodes),
        query_edges=len(query_edges),
        matched_nodes=sorted(node_map.values()),
    )


def recall_graph_candidates(
    *,
    query_graph: dict[str, Any],
    candidate_trajectory_ids: list[str],
    clean_graph_loader: Any,
) -> dict[str, GraphMatch]:
    out: dict[str, GraphMatch] = {}
    for tid in candidate_trajectory_ids:
        graph = clean_graph_loader(tid)
        if not isinstance(graph, dict):
            continue
        out[tid] = _mcs_match(query_graph, graph, trajectory_id=tid)
    return out
