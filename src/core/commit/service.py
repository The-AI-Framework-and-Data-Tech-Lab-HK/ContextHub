"""Commit service core logic for Phase 1."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

from core.commit.clean_deriver import derive_clean_graph
from core.commit.graph_builder import build_raw_graph
from core.commit.idempotency import commit_idempotency_key, trajectory_content_hash
from core.commit.normalizer import truncate_tool_output
from core.commit.pairing import pair_ai_tool_steps
from core.commit.summarizer import summarize_trajectory
from core.commit.validator import validate_raw_steps


@dataclass
class CommitCommand:
    tenant_id: str
    agent_id: str
    session_id: str
    task_id: str
    trajectory: list[dict[str, Any]]
    labels: dict[str, Any]
    is_incremental: bool = False
    trajectory_id: str | None = None
    visualize_graph_png: bool = False


@dataclass
class CommitResult:
    trajectory_id: str
    idempotency_key: str
    status: str
    nodes: int
    edges: int
    warnings: list[str]
    summary_l0: str
    summary_l1: str
    payload: dict[str, Any]


def _build_trajectory_id(cmd: CommitCommand) -> str:
    if cmd.trajectory_id:
        return cmd.trajectory_id
    h = trajectory_content_hash(cmd.trajectory)
    return f"traj_{h[:16]}"


def _normalize_steps(steps: list[dict[str, Any]], max_chars: int) -> tuple[list[dict[str, Any]], list[str]]:
    out: list[dict[str, Any]] = []
    warnings: list[str] = []
    for step in steps:
        s = dict(step)
        meta = dict(s.get("meta") or {})
        s["meta"] = meta
        role = meta.get("role")
        if role == "ToolMessage":
            # Guard against oversized payloads blowing up graph/storage size.
            ar = str(s.get("Action_result") or "")
            truncated, changed = truncate_tool_output(ar, max_chars=max_chars)
            s["Action_result"] = truncated
            if changed:
                warnings.append(f"step {s.get('Step')} Action_result truncated")
        out.append(s)
    return out, warnings


class CommitService:
    def __init__(
        self,
        *,
        max_action_result_chars: int = 12000,
        temporal_fallback_edge: bool = True,
        dataflow_extractor: Callable[..., list[dict[str, Any]] | dict[str, list[dict[str, Any]]]]
        | None = None,
        reasoning_min_confidence: float = 0.55,
    ) -> None:
        self.max_action_result_chars = max_action_result_chars
        self.temporal_fallback_edge = temporal_fallback_edge
        self.dataflow_extractor = dataflow_extractor
        self.reasoning_min_confidence = reasoning_min_confidence

    def run(self, cmd: CommitCommand) -> CommitResult:
        # 1) Validate request structure.
        validate_raw_steps(cmd.trajectory)
        # 2) Normalize heavy tool outputs.
        normalized_steps, warnings = _normalize_steps(
            cmd.trajectory, max_chars=self.max_action_result_chars
        )
        # 3) Build deterministic identifiers and idempotency key.
        trajectory_id = _build_trajectory_id(cmd)
        idem_key = commit_idempotency_key(cmd.tenant_id, cmd.task_id, normalized_steps)

        # 4) Convert trajectory -> paired actions -> raw/clean graphs.
        pairs = pair_ai_tool_steps(normalized_steps)
        raw_nodes, raw_edges = build_raw_graph(
            trajectory_id,
            pairs,
            temporal_fallback_edge=self.temporal_fallback_edge,
            dataflow_extractor=self.dataflow_extractor,
            reasoning_min_confidence=self.reasoning_min_confidence,
        )
        clean_nodes, clean_edges = derive_clean_graph(raw_nodes, raw_edges)
        # If LLM extractor is used, persist its latest extraction trace for debugging/audit.
        llm_traces: list[dict[str, Any]] | None = None
        extractor_obj = getattr(self.dataflow_extractor, "__self__", None) if self.dataflow_extractor else None
        if extractor_obj is not None and hasattr(extractor_obj, "last_traces"):
            raw_traces = getattr(extractor_obj, "last_traces")
            if isinstance(raw_traces, list):
                llm_traces = deepcopy([t for t in raw_traces if isinstance(t, dict)])
        # 5) Produce L0/L1 summaries for replay/indexing.
        l0, l1 = summarize_trajectory(normalized_steps)
        payload = {
            "trajectory": normalized_steps,
            "raw_graph": {
                "nodes": raw_nodes,
                "edges": raw_edges,
            },
            "clean_graph": {
                "nodes": clean_nodes,
                "edges": clean_edges,
            },
            "abstract": l0,
            "overview": l1,
            "task_id": cmd.task_id,
            "labels": cmd.labels,
            "nodes": len(raw_nodes),
            "edges": len(raw_edges),
        }
        if llm_traces:
            payload["llm_extraction_traces"] = llm_traces
        return CommitResult(
            trajectory_id=trajectory_id,
            idempotency_key=idem_key,
            status="accepted",
            nodes=len(raw_nodes),
            edges=len(raw_edges),
            warnings=warnings,
            summary_l0=l0,
            summary_l1=l1,
            payload=payload,
        )
