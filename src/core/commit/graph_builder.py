"""Build raw trajectory graph (nodes + typed edges) from paired steps."""

from __future__ import annotations

import math
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from core.commit.normalizer import parse_local_db_sql_action, parse_tool_output_to_dict
from core.commit.pairing import PairedActionNode


@dataclass
class GraphNode:
    node_id: str
    trajectory_id: str
    ai_step: int
    tool_step: int | None
    thinking: str
    tool_name: str | None
    tool_args: dict[str, Any] | None
    tool_output: dict[str, Any] | None
    output_status: str | None
    pending_output: bool
    quality_flags: list[str] = field(default_factory=list)


@dataclass
class GraphEdge:
    edge_id: str
    src: str
    dst: str
    dep_type: str
    signal: str | None
    confidence: float
    signal_detail: dict[str, Any] | None = None


_PATH_RE = re.compile(r"(/[\w\-/\.]+|\b[\w\-.]+\.(?:csv|json|sqlite|parquet|txt)\b)")
_ID_KEY_RE = re.compile(r"(?:^|[_\-.])(id|code|key|uuid)(?:$|[_\-.])", flags=re.IGNORECASE)
_SQL_TABLE_RE = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][\w$]*)", flags=re.IGNORECASE)
_SQL_COL_RE = re.compile(r"\bselect\s+(.+?)\s+\bfrom\b", flags=re.IGNORECASE | re.DOTALL)
_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}")
_STOPWORDS = frozenset(
    {
        "select",
        "from",
        "join",
        "where",
        "and",
        "or",
        "the",
        "for",
        "with",
        "into",
        "true",
        "false",
        "null",
        "none",
        "status",
        "data",
        "rows",
        "columns",
    }
)
DATAFLOW_THRESHOLD = 0.45
MAX_DATAFLOW_HITS_PER_DST = 2
ExtractorCallable = Callable[..., list[dict[str, Any]] | dict[str, list[dict[str, Any]]]]


def _infer_output_status(tool_output: dict[str, Any] | None, action_result_text: str) -> str | None:
    if tool_output and "status" in tool_output:
        value = str(tool_output.get("status", "")).lower()
        if value in {"success", "failed", "partial", "unknown"}:
            return value
    if not action_result_text:
        return None
    if re.search(r"['\"]status['\"]\s*:\s*['\"]failed['\"]", action_result_text):
        return "failed"
    if re.search(r"['\"]status['\"]\s*:\s*['\"]success['\"]", action_result_text):
        return "success"
    return "unknown"


@dataclass
class SignalSet:
    generic_tokens: set[str] = field(default_factory=set)
    path_tokens: set[str] = field(default_factory=set)
    table_tokens: set[str] = field(default_factory=set)
    column_tokens: set[str] = field(default_factory=set)
    id_tokens: set[str] = field(default_factory=set)


@dataclass
class DataflowHit:
    score: float
    evidence_type: str
    tokens: set[str]


def _flatten_values(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            out.extend(_flatten_values(v, key))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            key = f"{prefix}[{i}]"
            out.extend(_flatten_values(v, key))
    else:
        out.append((prefix, value))
    return out


def _extract_sql_tokens(text: str) -> tuple[set[str], set[str]]:
    tables = {m.group(1).lower() for m in _SQL_TABLE_RE.finditer(text)}
    cols: set[str] = set()
    m = _SQL_COL_RE.search(text)
    if m:
        segment = m.group(1)
        for raw in segment.split(","):
            token = raw.strip().split()[-1] if raw.strip() else ""
            token = token.strip("`\" ").lower()
            if token and token != "*":
                cols.add(token)
    return tables, cols


def extract_signals(payload: dict[str, Any] | None) -> SignalSet:
    sig = SignalSet()
    if not payload:
        return sig
    for key_path, raw in _flatten_values(payload):
        key_l = key_path.lower()
        if key_l:
            for t in _TOKEN_RE.findall(key_l):
                tl = t.lower()
                if tl not in _STOPWORDS:
                    sig.generic_tokens.add(tl)
            if _ID_KEY_RE.search(key_l):
                sig.id_tokens.add(key_l)

        text = str(raw).strip()
        if not text:
            continue
        text_l = text.lower()
        for match in _PATH_RE.finditer(text):
            sig.path_tokens.add(match.group(1).lower())
        tbs, cols = _extract_sql_tokens(text_l)
        sig.table_tokens.update(tbs)
        sig.column_tokens.update(cols)

        if isinstance(raw, (int, float, bool)):
            # Encode scalar with key path to avoid overmatching plain numbers.
            sig.generic_tokens.add(f"{key_l}={raw}".lower())
        else:
            for t in _TOKEN_RE.findall(text_l):
                if t not in _STOPWORDS:
                    sig.generic_tokens.add(t)

        # Direct ID-like scalar values get stronger matching ability.
        if (_ID_KEY_RE.search(key_l) or key_l.endswith(".id")) and len(text_l) >= 3:
            sig.id_tokens.add(text_l)
    return sig


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def _collect_scalar_values(value: Any, out: set[str]) -> None:
    if isinstance(value, dict):
        for v in value.values():
            _collect_scalar_values(v, out)
    elif isinstance(value, list):
        for v in value:
            _collect_scalar_values(v, out)
    else:
        text = str(value).strip()
        if text:
            out.add(text.lower())


def _normalize_match_token(text: str) -> str:
    return str(text).strip().strip("`'\"").lower()


def _output_blob(value: dict[str, Any] | None) -> str:
    if not value:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False).lower()
    except Exception:
        return str(value).lower()


def strip_echoed_output_payload(tool_output: dict[str, Any] | None, tool_args: dict[str, Any] | None) -> dict[str, Any] | None:
    """
    Remove output fields that only echo this node's own inputs.

    Example:
    tool_args.file_path == tool_output.db_path -> db_path removed from effective output.
    """
    if not tool_output:
        return None
    if not tool_args:
        return tool_output

    input_scalars: set[str] = set()
    _collect_scalar_values(tool_args, input_scalars)

    def _strip(value: Any) -> Any:
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for k, v in value.items():
                cv = _strip(v)
                if cv is None:
                    continue
                cleaned[k] = cv
            return cleaned or None
        if isinstance(value, list):
            cleaned_list = [cv for cv in (_strip(v) for v in value) if cv is not None]
            return cleaned_list or None
        text = str(value).strip().lower()
        if text and text in input_scalars:
            return None
        return value

    stripped = _strip(tool_output)
    if isinstance(stripped, dict):
        return stripped
    return {}


def subtract_existing_input_signals(
    output_sig: SignalSet,
    input_sig: SignalSet,
) -> SignalSet:
    """
    Remove signals in tool_output that are already present in this node's tool_args.

    This prevents "echoed inputs" (e.g. file_path echoed as db_path) from being treated
    as new outputs for downstream dataflow inference.
    """
    return SignalSet(
        generic_tokens=output_sig.generic_tokens - input_sig.generic_tokens,
        path_tokens=output_sig.path_tokens - input_sig.path_tokens,
        table_tokens=output_sig.table_tokens - input_sig.table_tokens,
        column_tokens=output_sig.column_tokens - input_sig.column_tokens,
        id_tokens=output_sig.id_tokens - input_sig.id_tokens,
    )


def match_signals(src: SignalSet, dst: SignalSet) -> DataflowHit | None:
    # Strong evidence: reusable path.
    path_hit = src.path_tokens & dst.path_tokens
    if path_hit:
        score = min(0.95, 0.85 + 0.05 * min(len(path_hit), 2))
        return DataflowHit(score=score, evidence_type="path", tokens=path_hit)

    # Strong evidence: table + column reuse.
    table_hit = src.table_tokens & dst.table_tokens
    col_hit = src.column_tokens & dst.column_tokens
    if table_hit and col_hit:
        score = min(0.92, 0.75 + 0.07 * min(len(table_hit) + len(col_hit), 3))
        return DataflowHit(score=score, evidence_type="table+column", tokens=table_hit | col_hit)
    if table_hit:
        score = min(0.82, 0.66 + 0.06 * min(len(table_hit), 3))
        return DataflowHit(score=score, evidence_type="table", tokens=table_hit)

    # Medium evidence: id/key propagation.
    id_hit = src.id_tokens & dst.id_tokens
    if id_hit:
        score = min(0.8, 0.62 + 0.05 * min(len(id_hit), 3))
        return DataflowHit(score=score, evidence_type="id/key", tokens=id_hit)

    # Weak generic evidence based on token overlap.
    generic_hit = src.generic_tokens & dst.generic_tokens
    if generic_hit:
        ratio = _jaccard(src.generic_tokens, dst.generic_tokens)
        if len(generic_hit) >= 2 or ratio >= 0.18:
            score = min(0.7, 0.35 + 0.35 * math.sqrt(ratio))
            return DataflowHit(score=score, evidence_type="token_overlap", tokens=generic_hit)
    return None


def build_raw_graph(
    trajectory_id: str,
    pairs: list[PairedActionNode],
    *,
    temporal_fallback_edge: bool = True,
    dataflow_extractor: ExtractorCallable | None = None,
    reasoning_min_confidence: float = 0.55,
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Create raw graph: dataflow, controlflow(retry), and optional temporal fallback."""
    nodes: list[GraphNode] = []
    for idx, p in enumerate(pairs):
        parsed = parse_local_db_sql_action(p.action) if p.action else None
        tool_name = parsed["tool_name"] if parsed else None
        tool_args = {k: v for k, v in (parsed or {}).items() if k != "tool_name"} or None
        tool_output = None if p.pending_output else parse_tool_output_to_dict(p.action_result)
        out_status = (
            _infer_output_status(tool_output, p.action_result or "")
            if not p.pending_output
            else None
        )

        node_id = f"{trajectory_id}-n{idx}"
        nodes.append(
            GraphNode(
                node_id=node_id,
                trajectory_id=trajectory_id,
                ai_step=p.ai_step,
                tool_step=p.tool_step,
                thinking=p.thinking,
                tool_name=tool_name,
                tool_args=tool_args,
                tool_output=tool_output,
                output_status=out_status,
                pending_output=p.pending_output,
                quality_flags=list(p.quality_flags),
            )
        )

    edges: list[GraphEdge] = []
    ei = 0
    dedup: set[tuple[str, str, str, str | None]] = set()

    def add_edge(
        src: str,
        dst: str,
        dep_type: str,
        signal: str | None,
        conf: float,
        signal_detail: dict[str, Any] | None = None,
    ) -> None:
        nonlocal ei
        key = (src, dst, dep_type, signal)
        if key in dedup:
            return
        dedup.add(key)
        edges.append(
            GraphEdge(
                edge_id=f"{trajectory_id}-e{ei}",
                src=src,
                dst=dst,
                dep_type=dep_type,
                signal=signal,
                signal_detail=signal_detail,
                confidence=conf,
            )
        )
        ei += 1

    incoming_dataflow: set[str] = set()

    if dataflow_extractor is not None:
        payload_nodes: list[dict[str, Any]] = []
        idx_of: dict[str, int] = {}
        output_blob_by_node: dict[str, str] = {}
        for idx, n in enumerate(nodes):
            idx_of[n.node_id] = idx
            effective_out = strip_echoed_output_payload(n.tool_output, n.tool_args)
            payload_nodes.append(
                {
                    "node_id": n.node_id,
                    "ai_step": n.ai_step,
                    "tool_step": n.tool_step,
                    "tool_name": n.tool_name,
                    "tool_args": n.tool_args,
                    "tool_output": n.tool_output,
                    "effective_tool_output": effective_out,
                    "output_status": n.output_status,
                }
            )
            output_blob_by_node[n.node_id] = _output_blob(effective_out)
        try:
            try:
                llm_result = dataflow_extractor(
                    nodes=payload_nodes,
                    threshold=DATAFLOW_THRESHOLD,
                    top_k_per_dst=MAX_DATAFLOW_HITS_PER_DST,
                    reasoning_threshold=reasoning_min_confidence,
                )
            except TypeError:
                # Backward compatibility for older extractors without reasoning_threshold.
                llm_result = dataflow_extractor(
                    nodes=payload_nodes,
                    threshold=DATAFLOW_THRESHOLD,
                    top_k_per_dst=MAX_DATAFLOW_HITS_PER_DST,
                )
        except Exception:
            llm_result = []

        # Backward compatibility: old extractor may return plain list[dataflow_edge].
        if isinstance(llm_result, list):
            llm_dataflow_edges = llm_result
            llm_reasoning_edges: list[dict[str, Any]] = []
        else:
            llm_dataflow_edges = llm_result.get("dataflow_edges") or []
            llm_reasoning_edges = llm_result.get("reasoning_edges") or []

        for e in llm_dataflow_edges:
            src_id = str(e.get("src_node_id", ""))
            dst_id = str(e.get("dst_node_id", ""))
            if src_id not in idx_of or dst_id not in idx_of:
                continue
            if idx_of[src_id] >= idx_of[dst_id]:
                continue
            conf = float(e.get("confidence", 0.0) or 0.0)
            if conf < DATAFLOW_THRESHOLD:
                continue
            evidence = str(e.get("evidence_type") or "llm")
            matched_tokens = e.get("matched_tokens") or []
            if not isinstance(matched_tokens, list):
                matched_tokens = [str(matched_tokens)]
            src_blob = output_blob_by_node.get(src_id, "")
            filtered_tokens = [
                str(t)
                for t in matched_tokens
                if _normalize_match_token(str(t))
                and _normalize_match_token(str(t)) in src_blob
            ]
            # Strict provenance guard:
            # only accept LLM edges when at least one matched token is present in
            # source effective tool output (not merely in source tool args).
            if not filtered_tokens:
                continue
            add_edge(
                src_id,
                dst_id,
                "dataflow",
                evidence,
                conf,
                signal_detail={
                    "matched_tokens": filtered_tokens,
                    "evidence_type": evidence,
                    "reason": str(e.get("reason") or ""),
                    "source": "llm",
                },
            )
            incoming_dataflow.add(dst_id)

        for e in llm_reasoning_edges:
            src_id = str(e.get("src_node_id", ""))
            dst_id = str(e.get("dst_node_id", ""))
            if src_id not in idx_of or dst_id not in idx_of:
                continue
            if idx_of[src_id] >= idx_of[dst_id]:
                continue
            conf = float(e.get("confidence", 0.0) or 0.0)
            if conf < reasoning_min_confidence:
                continue
            matched_evidence = e.get("matched_evidence") or []
            if not isinstance(matched_evidence, list):
                matched_evidence = [str(matched_evidence)]
            # NOTE:
            # For reasoning edges, we intentionally do not hard-filter by
            # token presence in src output / dst thinking. LLM may summarize
            # evidence at a semantic level rather than verbatim token match.
            # We keep matched_evidence as provided for explainability.
            filtered_evidence = [str(ev) for ev in matched_evidence if str(ev).strip()]
            add_edge(
                src_id,
                dst_id,
                "reasoning",
                "thinking_reference",
                conf,
                signal_detail={
                    "matched_evidence": filtered_evidence,
                    "reason_summary": str(e.get("reason_summary") or ""),
                    "source": "llm",
                },
            )
    else:
        # Rule-based dataflow extraction: match src tool_output -> dst tool_args.
        for j in range(1, len(nodes)):
            dst = nodes[j]
            if not dst.tool_args:
                continue

            dst_sig = extract_signals(dst.tool_args)
            hits: list[tuple[GraphNode, DataflowHit]] = []
            for i in range(j):
                src = nodes[i]
                if not src.tool_output:
                    continue
                src_out_sig = extract_signals(src.tool_output)
                src_in_sig = extract_signals(src.tool_args)
                src_sig = subtract_existing_input_signals(src_out_sig, src_in_sig)
                hit = match_signals(src_sig, dst_sig)
                if hit and hit.score >= DATAFLOW_THRESHOLD:
                    hits.append((src, hit))

            if hits:
                hits.sort(key=lambda x: x[1].score, reverse=True)
                for src, hit in hits[:MAX_DATAFLOW_HITS_PER_DST]:
                    add_edge(
                        src.node_id,
                        dst.node_id,
                        "dataflow",
                        hit.evidence_type,
                        hit.score,
                        signal_detail={
                            "matched_tokens": sorted(hit.tokens),
                            "evidence_type": hit.evidence_type,
                        },
                    )
                    incoming_dataflow.add(dst.node_id)

    # Temporal fallback is evaluated after dataflow extraction.
    if temporal_fallback_edge:
        for j in range(1, len(nodes)):
            dst = nodes[j]
            if dst.node_id in incoming_dataflow:
                continue
            add_edge(
                nodes[j - 1].node_id,
                dst.node_id,
                "temporal",
                None,
                0.2,
                signal_detail={"reason": "no_dataflow_hit_above_threshold"},
            )

    for i in range(1, len(nodes)):
        prev, cur = nodes[i - 1], nodes[i]
        if (
            prev.output_status == "failed"
            and cur.tool_name
            and prev.tool_name == cur.tool_name
            and prev.tool_args
            and cur.tool_args
            and prev.tool_args.get("file_path") == cur.tool_args.get("file_path")
        ):
            add_edge(
                prev.node_id,
                cur.node_id,
                "controlflow",
                "retry",
                0.9,
                signal_detail={"reason": "failed_then_same_tool_retry"},
            )

    return nodes, edges
