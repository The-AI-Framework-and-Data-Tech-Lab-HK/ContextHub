"""Render raw/clean graph into PNG files for local inspection."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any


def render_graph_png(graph: dict[str, Any], output_path: str, title: str) -> None:
    """
    Render a simple directed graph PNG.

    - Node label: tool_name + (ai_step / tool_step)
    - Edge label: hidden (per requirement)
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:  # pragma: no cover - depends on optional matplotlib
        raise RuntimeError(
            "Graph PNG visualization requires matplotlib. "
            "Please install it before enabling visualize_graph_png."
        ) from e

    nodes = list(graph.get("nodes") or [])
    edges = list(graph.get("edges") or [])
    if not nodes:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.set_title(title)
        ax.text(0.5, 0.5, "No nodes", ha="center", va="center")
        ax.axis("off")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, dpi=180, bbox_inches="tight")
        plt.close(fig)
        return

    node_ids = [str(n.get("node_id")) for n in nodes]
    labels: dict[str, str] = {}
    for n in nodes:
        nid = str(n.get("node_id"))
        tool_name = str(n.get("tool_name") or "unknown_tool")
        ai_step = n.get("ai_step")
        tool_step = n.get("tool_step")
        ai_str = str(ai_step) if ai_step is not None else "-"
        tool_str = str(tool_step) if tool_step is not None else "-"
        labels[nid] = f"{tool_name}\nai:{ai_str} / tool:{tool_str}"

    # Circular deterministic layout (no extra dependencies such as networkx).
    total = len(node_ids)
    radius = max(2.5, min(8.0, total / 2.0))
    positions: dict[str, tuple[float, float]] = {}
    for i, nid in enumerate(node_ids):
        angle = 2 * math.pi * i / total
        positions[nid] = (radius * math.cos(angle), radius * math.sin(angle))

    fig, ax = plt.subplots(figsize=(8, 8))
    ax.set_title(title)

    # Draw directed edges with dep_type color. Only dataflow edges show confidence.
    for e in edges:
        src = str(e.get("src"))
        dst = str(e.get("dst"))
        if src not in positions or dst not in positions:
            continue
        dep_type = str(e.get("dep_type") or "")
        confidence = float(e.get("confidence", 0.0) or 0.0)
        if dep_type == "dataflow":
            edge_color = "#2ca02c"
        elif dep_type == "temporal":
            edge_color = "#999999"
        elif dep_type == "controlflow":
            edge_color = "#d62728"
        else:
            edge_color = "#1f77b4"
        x1, y1 = positions[src]
        x2, y2 = positions[dst]
        ax.annotate(
            "",
            xy=(x2, y2),
            xytext=(x1, y1),
            arrowprops={
                "arrowstyle": "-|>",
                "linewidth": 1.5,
                "alpha": 0.9,
                "color": edge_color,
                "mutation_scale": 16,
                "shrinkA": 18,
                "shrinkB": 18,
            },
        )
        if dep_type == "dataflow":
            detail = e.get("signal_detail") or {}
            tokens = detail.get("matched_tokens") or []
            token_text = ""
            if isinstance(tokens, list) and tokens:
                shown = [str(t) for t in tokens[:3]]
                token_text = ", ".join(shown)
                if len(tokens) > 3:
                    token_text += ", ..."
            mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            ax.text(
                mx,
                my,
                f"{confidence:.2f}\n{token_text}" if token_text else f"{confidence:.2f}",
                fontsize=7,
                color=edge_color,
                ha="center",
                va="center",
                bbox={"boxstyle": "round,pad=0.18", "facecolor": "white", "alpha": 0.8, "edgecolor": "none"},
                zorder=5,
            )

    # Draw nodes + labels (tool_name + ai_step/tool_step).
    for nid in node_ids:
        x, y = positions[nid]
        ax.scatter([x], [y], s=980, color="#cfe8ff", edgecolors="#2f5597", linewidths=1.5, zorder=3)
        ax.text(x, y, labels[nid], ha="center", va="center", fontsize=7.5, zorder=4)

    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(fig)

