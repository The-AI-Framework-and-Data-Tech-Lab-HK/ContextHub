"""Run retrieve from command line and print latency stats."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

from app.config import load_settings
from app.orchestrators.retrieve_orchestrator import RetrieveOrchestrator
from core.retrieve.semantic_recall import SemanticRecall
from core.retrieve.service import RetrieveCommand, RetrieveService
from infra.audit.audit_logger import JsonlAuditLogger
from infra.storage.fs.trajectory_repo import LocalFSTrajectoryRepository
from infra.storage.graph.factory import build_graph_store_writer
from infra.storage.vector.factory import build_vector_store_adapter


def _load_partial_trajectory(path: Path | None) -> list[dict[str, Any]] | None:
    if path is None:
        return None
    if not path.exists():
        raise FileNotFoundError(f"partial trajectory file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("partial trajectory JSON must be a list of step objects")
    return data


def _parse_tool_whitelist(raw: str) -> list[str]:
    if not raw.strip():
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = int(round((len(sorted_values) - 1) * p))
    idx = max(0, min(idx, len(sorted_values) - 1))
    return float(sorted_values[idx])


def _clean_graph_stats(clean_graph: Any) -> dict[str, Any]:
    if not isinstance(clean_graph, dict):
        return {"source": "neo4j", "nodes": 0, "edges": 0, "edge_type_counts": {}}
    nodes = clean_graph.get("nodes")
    edges = clean_graph.get("edges")
    node_list = nodes if isinstance(nodes, list) else []
    edge_list = edges if isinstance(edges, list) else []
    dep_counts: dict[str, int] = {}
    for e in edge_list:
        if not isinstance(e, dict):
            continue
        dep = str(e.get("dep_type") or "unknown")
        dep_counts[dep] = dep_counts.get(dep, 0) + 1
    return {
        "source": "neo4j",
        "nodes": len(node_list),
        "edges": len(edge_list),
        "edge_type_counts": dep_counts,
    }


def run_retrieve(
    *,
    tenant_id: str,
    agent_id: str,
    task_description: str,
    task_type: str | None,
    tool_whitelist: list[str],
    partial_trajectory_file: Path | None,
    top_k: int,
    repeat: int,
    include_clean_graph: bool = False,
    include_graph_match_details: bool = False,
    config_path: str | None = None,
) -> dict[str, Any]:
    settings = load_settings(config_path=config_path)
    repo = LocalFSTrajectoryRepository(root=settings.storage.localfs_root)
    audit = JsonlAuditLogger(file_path=settings.storage.audit_file_path)
    graph_store = build_graph_store_writer(settings)
    vector_store = build_vector_store_adapter(settings)
    semantic = None
    if vector_store is not None and settings.embedding_provider.lower() == "openai" and settings.openai_api_key:
        semantic = SemanticRecall(
            vector_store=vector_store,
            embedding_model=settings.embedding_model,
            api_key=settings.openai_api_key,
            embedder_base_url=settings.model_endpoints.embedder_base_url or None,
            embedding_mode=settings.embedding_mode,
        )

    clean_graph_loader = None
    if graph_store is not None and hasattr(graph_store, "load_clean_graph"):
        clean_graph_loader = lambda trajectory_id: graph_store.load_clean_graph(trajectory_id=trajectory_id)  # type: ignore[attr-defined]
    orchestrator = RetrieveOrchestrator(
        retrieve_service=RetrieveService(
            semantic_recall=semantic,
            clean_graph_loader=clean_graph_loader,
        ),
        repo=repo,
        audit=audit,
        clean_graph_loader=clean_graph_loader,
    )
    partial = _load_partial_trajectory(partial_trajectory_file)
    query_payload = {
        "task_description": task_description,
        "partial_trajectory": partial,
        "constraints": {"tool_whitelist": tool_whitelist},
        "task_type": task_type,
    }

    repeats = max(1, int(repeat))
    latencies_ms: list[float] = []
    last_result: dict[str, Any] = {"items": [], "warnings": []}
    for _ in range(repeats):
        t0 = time.perf_counter()
        result = orchestrator.retrieve(
            RetrieveCommand(
                tenant_id=tenant_id,
                agent_id=agent_id,
                query=query_payload,
                top_k=top_k,
            )
        )
        elapsed = (time.perf_counter() - t0) * 1000.0
        latencies_ms.append(elapsed)
        items = result.items
        if not include_clean_graph:
            compact_items: list[dict[str, Any]] = []
            for item in items:
                out = dict(item)
                if not include_graph_match_details:
                    evidence = out.get("evidence")
                    if isinstance(evidence, dict):
                        evidence = dict(evidence)
                        evidence.pop("graph_match", None)
                        out["evidence"] = evidence
                cg = out.pop("clean_graph", None)
                out["clean_graph_stats"] = _clean_graph_stats(cg)
                compact_items.append(out)
            items = compact_items
        last_result = {"items": items, "warnings": result.warnings}

    sorted_lat = sorted(latencies_ms)
    perf = {
        "runs": repeats,
        "latency_ms": {
            "min": min(sorted_lat),
            "max": max(sorted_lat),
            "mean": statistics.fmean(sorted_lat),
            "p50": _percentile(sorted_lat, 0.50),
            "p95": _percentile(sorted_lat, 0.95),
            "p99": _percentile(sorted_lat, 0.99),
        },
    }
    return {
        "query": {
            "task_description": task_description,
        },
        "performance": perf,
        "result": last_result,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amc-retrieve-trajectory",
        description="Run retrieve pipeline and print semantic result + latency stats.",
    )
    parser.add_argument("--tenant-id", default="tenant-local", help="Tenant identifier")
    parser.add_argument("--agent-id", default="agent-local", help="Agent identifier")
    parser.add_argument("--task-description", required=True, help="Retrieve query task description text")
    parser.add_argument("--task-type", default="", help="Optional task_type hint")
    parser.add_argument(
        "--tool-whitelist",
        default="",
        help="Optional comma-separated tool names, e.g. local_db_sql,read_report",
    )
    parser.add_argument(
        "--partial-trajectory-file",
        default=None,
        help="Optional partial trajectory JSON path",
    )
    parser.add_argument("--top-k", type=int, default=5, help="Top-K trajectories to return")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat runs for latency stats")
    parser.add_argument(
        "--include-clean-graph",
        action="store_true",
        help="Include full clean_graph payload in output (default: false; print neo4j stats only).",
    )
    parser.add_argument(
        "--include-graph-match-details",
        action="store_true",
        help="Include detailed graph_match evidence in output (default: false).",
    )
    parser.add_argument("--config-path", default=None, help="Optional config YAML path")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    out = run_retrieve(
        tenant_id=args.tenant_id,
        agent_id=args.agent_id,
        task_description=args.task_description,
        task_type=args.task_type or None,
        tool_whitelist=_parse_tool_whitelist(args.tool_whitelist),
        partial_trajectory_file=Path(args.partial_trajectory_file) if args.partial_trajectory_file else None,
        top_k=int(args.top_k),
        repeat=int(args.repeat),
        include_clean_graph=bool(args.include_clean_graph),
        include_graph_match_details=bool(args.include_graph_match_details),
        config_path=args.config_path,
    )
    if args.pretty:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
