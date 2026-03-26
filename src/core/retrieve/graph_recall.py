"""Graph recall with MCS-like matching for partial trajectories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import networkx as nx
from networkx.algorithms import isomorphism

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


def _to_nx_digraph(graph: dict[str, Any] | None) -> nx.DiGraph:
    nodes, edges = _normalized_graph(graph)
    g = nx.DiGraph()
    valid_ids: set[str] = set()
    for n in nodes:
        nid = str(n.get("node_id") or "")
        if not nid:
            continue
        valid_ids.add(nid)
        g.add_node(nid, action=_node_label(n))
    for e in edges:
        src = str(e.get("src") or "")
        dst = str(e.get("dst") or "")
        dep = _edge_type(e)
        if not src or not dst or not dep:
            continue
        if src not in valid_ids or dst not in valid_ids:
            continue
        g.add_edge(src, dst, dep_type=dep)
    return g


def _mcs_match(query_graph: dict[str, Any], candidate_graph: dict[str, Any], *, trajectory_id: str) -> GraphMatch:
    qg = _to_nx_digraph(query_graph)
    cg = _to_nx_digraph(candidate_graph)
    if qg.number_of_nodes() == 0:
        return GraphMatch(
            trajectory_id=trajectory_id,
            graph_score=0.0,
            matched_mcs_nodes=0,
            matched_mcs_edges=0,
            query_nodes=0,
            query_edges=0,
            matched_nodes=[],
        )

    node_match = isomorphism.categorical_node_match("action", "")
    edge_match = isomorphism.categorical_edge_match("dep_type", "")

    # Mapping direction: candidate_node_id -> query_node_id
    # (because ISMAGS is instantiated with (candidate_graph, query_graph)).
    matcher = isomorphism.ISMAGS(cg, qg, node_match=node_match, edge_match=edge_match)
    best_mapping: dict[str, str] | None = next(matcher.largest_common_subgraph(), None)
    if not best_mapping:
        return GraphMatch(
            trajectory_id=trajectory_id,
            graph_score=0.0,
            matched_mcs_nodes=0,
            matched_mcs_edges=0,
            query_nodes=qg.number_of_nodes(),
            query_edges=qg.number_of_edges(),
            matched_nodes=[],
        )

    matched_nodes = len(best_mapping)
    inv_map = {q: c for c, q in best_mapping.items()}
    matched_edges = 0
    for q_src, q_dst, q_data in qg.edges(data=True):
        c_src = inv_map.get(q_src)
        c_dst = inv_map.get(q_dst)
        if not c_src or not c_dst:
            continue
        if not cg.has_edge(c_src, c_dst):
            continue
        c_data = cg.get_edge_data(c_src, c_dst) or {}
        if str(c_data.get("dep_type") or "") == str(q_data.get("dep_type") or ""):
            matched_edges += 1

    denom = qg.number_of_nodes() + qg.number_of_edges()
    score = float(matched_nodes + matched_edges) / float(max(1, denom))
    return GraphMatch(
        trajectory_id=trajectory_id,
        graph_score=score,
        matched_mcs_nodes=matched_nodes,
        matched_mcs_edges=matched_edges,
        query_nodes=qg.number_of_nodes(),
        query_edges=qg.number_of_edges(),
        matched_nodes=sorted(best_mapping.keys()),
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
