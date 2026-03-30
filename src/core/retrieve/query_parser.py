"""Query parser for semantic retrieve."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ParsedRetrieveQuery:
    query_text: str
    task_type: str | None
    has_partial_trajectory: bool
    tool_whitelist: tuple[str, ...]


def _extract_failure_clues(partial_trajectory: list[dict[str, Any]] | None) -> list[str]:
    if not partial_trajectory:
        return []
    clues: list[str] = []
    for step in partial_trajectory:
        text = str(step.get("Action_result") or "")
        lowered = text.lower()
        if any(k in lowered for k in ["error", "failed", "exception", "traceback", "syntax"]):
            clues.append(text[:200].replace("\n", " ").strip())
    return clues[:3]


def parse_retrieve_query(query: dict[str, Any]) -> ParsedRetrieveQuery:
    task_description = str(query.get("task_description") or "").strip()
    task_type_raw = str(query.get("task_type") or "").strip()
    task_type = task_type_raw or None
    constraints = query.get("constraints") or {}
    tool_whitelist_raw = constraints.get("tool_whitelist") if isinstance(constraints, dict) else []
    tool_whitelist = tuple(str(x).strip() for x in (tool_whitelist_raw or []) if str(x).strip())
    partial = query.get("partial_trajectory")
    partial_steps = partial if isinstance(partial, list) else None
    failure_clues = _extract_failure_clues(partial_steps)

    parts: list[str] = []
    if task_description:
        parts.append(task_description)
    if task_type:
        parts.append(f"task_type: {task_type}")
    if tool_whitelist:
        parts.append("tools: " + ", ".join(tool_whitelist))
    if failure_clues:
        parts.append("failure clues: " + " | ".join(failure_clues))

    query_text = " || ".join(parts).strip() or "retrieve similar trajectories"
    return ParsedRetrieveQuery(
        query_text=query_text,
        task_type=task_type,
        has_partial_trajectory=bool(partial_steps),
        tool_whitelist=tool_whitelist,
    )
