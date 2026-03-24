"""Pair AIMessage steps with ToolMessage results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.commit.normalizer import parse_local_db_sql_action, parse_tool_output_to_dict


@dataclass
class PairedActionNode:
    """One action unit: one AIMessage + optional ToolMessage (see AMC_plan/02)."""

    ai_step: int
    tool_step: int | None
    thinking: str
    action: str
    action_result: str
    pending_output: bool = False
    quality_flags: list[str] = field(default_factory=list)


def _role(step: dict[str, Any]) -> str:
    return (step.get("meta") or {}).get("role") or ""


def _tool_name_from_action(action: str) -> str | None:
    parsed = parse_local_db_sql_action(action or "")
    if not parsed:
        return None
    return str(parsed.get("tool_name") or "") or None


def _compatibility_score(tool_name: str | None, tool_output: dict[str, Any] | None) -> int:
    """
    Score whether a ToolMessage output looks compatible with an AI action tool type.

    Higher score means better match. 0 means no specific evidence.
    """
    if not tool_output:
        return 0
    keys = set(tool_output.keys())
    if not tool_name:
        return 1 if "status" in keys else 0

    name = tool_name.lower()
    if name == "local_db_sql":
        if keys & {"db_path", "rows", "columns", "data", "error"}:
            return 3
        return 1 if "status" in keys else 0
    if name == "write_report":
        return 3 if keys & {"path", "bytes"} else (1 if "status" in keys else 0)
    if name == "plot_table":
        return 3 if "image_path" in keys else (1 if "status" in keys else 0)
    if name == "terminate":
        return 3 if "output" in keys else (1 if "status" in keys else 0)
    return 1 if "status" in keys else 0


def _pick_ai_index_for_tool(
    pairs: list[PairedActionNode],
    open_pair_indices: list[int],
    tool_step: dict[str, Any],
) -> int:
    """
    Pick one unmatched AI node for this tool output.

    Strategy:
    - Prefer higher compatibility score based on tool name and output shape.
    - For ties, prefer earlier unmatched AI (FIFO), preserving batched semantics.
    """
    out = parse_tool_output_to_dict(tool_step.get("Action_result"))
    best_pos = 0
    best_score = -1
    for pos, pair_idx in enumerate(open_pair_indices):
        p = pairs[pair_idx]
        score = _compatibility_score(_tool_name_from_action(p.action), out)
        if score > best_score:
            best_score = score
            best_pos = pos
    return best_pos


def pair_ai_tool_steps(steps: list[dict[str, Any]]) -> list[PairedActionNode]:
    """
    Stream-preserving pairing with compatibility-aware matching.

    - Works for strict alternation (AI -> Tool).
    - Works for batched execution (many AI, then many Tool outputs).
    - When multiple unmatched AI actions exist, chooses the most output-compatible one.
    - Remaining AIMessages without a matching Tool get ``pending_output=True``.
    """
    pairs: list[PairedActionNode] = []
    open_pair_indices: list[int] = []
    for step in steps:
        role = _role(step)
        if role == "AIMessage":
            flags = ["pending_output"]
            pairs.append(
                PairedActionNode(
                    ai_step=int(step["Step"]),
                    tool_step=None,
                    thinking=str(step.get("Thinking") or ""),
                    action=str(step.get("Action") or ""),
                    action_result="",
                    pending_output=True,
                    quality_flags=flags,
                )
            )
            open_pair_indices.append(len(pairs) - 1)
        elif role == "ToolMessage":
            if not open_pair_indices:
                continue
            pos = _pick_ai_index_for_tool(pairs, open_pair_indices, step)
            pair_idx = open_pair_indices.pop(pos)
            p = pairs[pair_idx]
            p.tool_step = int(step["Step"])
            p.action_result = str(step.get("Action_result") or "")
            p.pending_output = False
            p.quality_flags = [f for f in p.quality_flags if f != "pending_output"]
    return pairs
