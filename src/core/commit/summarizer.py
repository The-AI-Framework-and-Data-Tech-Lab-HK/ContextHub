"""Trajectory-level L0/L1 summary helpers for Phase 1."""

from __future__ import annotations

from typing import Any, Callable


def _summarize_rule_based(steps: list[dict[str, Any]]) -> tuple[str, str]:
    # L0: short one-liner for quick preview/recall metadata.
    ai_actions = [str(s.get("Action") or "") for s in steps if (s.get("meta") or {}).get("role") == "AIMessage"]
    tool_results = [
        str(s.get("Action_result") or "") for s in steps if (s.get("meta") or {}).get("role") == "ToolMessage"
    ]

    tools: list[str] = []
    for action in ai_actions:
        if action.startswith("local_db_sql("):
            tools.append("local_db_sql")
        elif action:
            tools.append("unknown_tool")
    unique_tools = sorted(set(tools))
    l0 = f"Trajectory with {len(ai_actions)} actions, tools={','.join(unique_tools) or 'none'}."

    failures = sum(1 for r in tool_results if "'status': 'failed'" in r or '"status": "failed"' in r)
    successes = sum(1 for r in tool_results if "'status': 'success'" in r or '"status": "success"' in r)
    # L1: slightly richer execution statistics for replay/introspection.
    l1 = (
        f"Steps={len(steps)}, AI actions={len(ai_actions)}, tool_results={len(tool_results)}, "
        f"successes={successes}, failures={failures}. "
        "Phase1 summary for commit/replay and vector indexing."
    )
    return l0, l1


def summarize_trajectory(
    steps: list[dict[str, Any]],
    llm_summarizer: Callable[[list[dict[str, Any]]], tuple[str, str]] | None = None,
) -> tuple[str, str]:
    """
    Prefer LLM summary when available; fall back to deterministic rule-based summary.
    """
    if llm_summarizer is not None:
        try:
            l0, l1 = llm_summarizer(steps)
            if str(l0).strip() and str(l1).strip():
                return str(l0), str(l1)
        except Exception:
            pass
    return _summarize_rule_based(steps)
