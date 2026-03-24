"""Derive clean graph from raw graph (AMC_plan/02 §2.4, MVP rules)."""

from __future__ import annotations

from copy import deepcopy

from core.commit.graph_builder import GraphEdge, GraphNode


def derive_clean_graph(
    raw_nodes: list[GraphNode],
    raw_edges: list[GraphEdge],
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """
    Remove failed nodes that are superseded by a later successful same-tool same-file call.

    Keeps pending_output nodes (no verdict yet). Edge set is derived by filtering raw
    edges whose endpoints are still present, preserving dep_type/signal/confidence.
    """
    # Mark failed nodes that have a later successful same tool+file
    remove_ids: set[str] = set()
    for i, n in enumerate(raw_nodes):
        if n.output_status != "failed":
            continue
        for j in range(i + 1, len(raw_nodes)):
            m = raw_nodes[j]
            if m.output_status != "success":
                continue
            if n.tool_name and n.tool_name == m.tool_name:
                nfp = (n.tool_args or {}).get("file_path")
                mfp = (m.tool_args or {}).get("file_path")
                if nfp and nfp == mfp:
                    remove_ids.add(n.node_id)
                    break

    kept = [deepcopy(n) for n in raw_nodes if n.node_id not in remove_ids]
    kept_ids = {n.node_id for n in kept}

    # Keep edge semantics and confidence from raw graph.
    tid = kept[0].trajectory_id if kept else "unknown"
    edges: list[GraphEdge] = []
    for i, e in enumerate(raw_edges):
        if e.src not in kept_ids or e.dst not in kept_ids:
            continue
        edges.append(
            GraphEdge(
                edge_id=f"{tid}-clean-e{i}",
                src=e.src,
                dst=e.dst,
                dep_type=e.dep_type,
                signal=e.signal,
                confidence=e.confidence,
                signal_detail=e.signal_detail,
            )
        )

    return kept, edges
