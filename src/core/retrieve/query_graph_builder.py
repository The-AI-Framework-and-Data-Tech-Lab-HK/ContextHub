"""Build query graph from partial trajectory for graph recall."""

from __future__ import annotations

from typing import Any

from core.commit.clean_deriver import derive_clean_graph
from core.commit.graph_builder import build_raw_graph
from core.commit.pairing import pair_ai_tool_steps


def _to_graph_dict(nodes: list[Any], edges: list[Any]) -> dict[str, list[dict[str, Any]]]:
    out_nodes: list[dict[str, Any]] = []
    out_edges: list[dict[str, Any]] = []
    for n in nodes:
        out_nodes.append(
            {
                "node_id": str(getattr(n, "node_id", "") or ""),
                "tool_name": getattr(n, "tool_name", None),
                "ai_step": int(getattr(n, "ai_step", 0) or 0),
            }
        )
    for e in edges:
        out_edges.append(
            {
                "edge_id": str(getattr(e, "edge_id", "") or ""),
                "src": str(getattr(e, "src", "") or ""),
                "dst": str(getattr(e, "dst", "") or ""),
                "dep_type": str(getattr(e, "dep_type", "") or ""),
            }
        )
    return {"nodes": out_nodes, "edges": out_edges}


def build_query_graph(
    *,
    partial_trajectory: list[dict[str, Any]],
    trajectory_id: str = "query",
) -> dict[str, list[dict[str, Any]]] | None:
    """
    Convert partial trajectory steps into a clean graph dict.

    This reuses commit-side pairing + graph derivation logic so graph schema and
    edge semantics stay aligned with stored clean graphs in Neo4j.
    """
    if not partial_trajectory:
        return None
    pairs = pair_ai_tool_steps(partial_trajectory)
    if not pairs:
        return None
    raw_nodes, raw_edges = build_raw_graph(
        trajectory_id=trajectory_id,
        pairs=pairs,
        temporal_fallback_edge=True,
        dataflow_extractor=None,
    )
    clean_nodes, clean_edges = derive_clean_graph(raw_nodes, raw_edges)
    graph = _to_graph_dict(clean_nodes, clean_edges)
    if not graph["nodes"]:
        return None
    return graph
