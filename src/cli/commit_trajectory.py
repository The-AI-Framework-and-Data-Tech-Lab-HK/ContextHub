"""Commit one trajectory JSON from command line and print stored artifact paths."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.config import load_settings
from app.orchestrators.commit_orchestrator import CommitOrchestrator
from core.commit.dataflow_llm import LLMDataflowExtractor
from core.commit.service import CommitCommand, CommitService
from core.indexing.trajectory_vector_indexer import TrajectoryVectorIndexer
from core.commit.summary_llm import LLMTrajectorySummarizer
from infra.audit.audit_logger import JsonlAuditLogger
from infra.storage.fs.trajectory_repo import LocalFSTrajectoryRepository
from infra.storage.graph.factory import build_graph_store_writer
from infra.storage.vector.factory import build_vector_store_adapter


def _load_trajectory(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"trajectory file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("trajectory JSON must be a list of step objects")
    return data


def run_commit(
    *,
    trajectory_file: Path,
    tenant_id: str,
    agent_id: str,
    session_id: str,
    task_id: str | None,
    task_type: str,
    trajectory_id: str | None,
    visualize_graph_png: bool = False,
    disable_idempotency: bool = False,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Execute Phase 1 commit pipeline and return a CLI-friendly result payload."""
    settings = load_settings(config_path=config_path)
    repo = LocalFSTrajectoryRepository(root=settings.storage.localfs_root)
    audit = JsonlAuditLogger(file_path=settings.storage.audit_file_path)
    graph_store = build_graph_store_writer(settings)
    vector_indexer = None
    if (
        settings.indexing_async_enabled
        and settings.embedding_provider.lower() == "openai"
        and settings.openai_api_key
    ):
        vector_store = build_vector_store_adapter(settings)
        if vector_store is not None and settings.indexing_include_levels:
            vector_indexer = TrajectoryVectorIndexer(
                vector_store=vector_store,
                embedding_model=settings.embedding_model,
                api_key=settings.openai_api_key,
                embedder_base_url=settings.model_endpoints.embedder_base_url or None,
                embedding_mode=settings.embedding_mode,
                include_levels=tuple(int(x) for x in settings.indexing_include_levels),
            )
    dataflow_extractor = None
    llm_summarizer = None
    if settings.openai_api_key:
        llm_summary = LLMTrajectorySummarizer(
            api_key=settings.openai_api_key,
            model=settings.llm_model,
            base_url=settings.model_endpoints.llm_base_url or None,
            temperature=0.0,
        )
        llm_summarizer = llm_summary.summarize
    if settings.commit.dataflow_extractor.lower() == "llm":
        if settings.openai_api_key:
            llm_extractor = LLMDataflowExtractor(
                api_key=settings.openai_api_key,
                model=settings.llm_model,
                base_url=settings.model_endpoints.llm_base_url or None,
                temperature=settings.commit.dataflow_llm_temperature,
            )
            dataflow_extractor = llm_extractor.extract
        else:
            print(
                "[AMC] dataflow_extractor=llm but AMC_OPENAI_API_KEY is empty, fallback to rule_based."
            )

    service = CommitService(
        max_action_result_chars=settings.commit.max_action_result_chars,
        temporal_fallback_edge=settings.commit.temporal_fallback_edge,
        dataflow_extractor=dataflow_extractor,
        llm_summarizer=llm_summarizer,
        reasoning_min_confidence=settings.commit.reasoning_min_confidence,
    )
    idempotency_enabled = (
        False if disable_idempotency else settings.commit.idempotency_enabled
    )
    orchestrator = CommitOrchestrator(
        commit_service=service,
        repo=repo,
        audit=audit,
        graph_store=graph_store,
        vector_indexer=vector_indexer,
        idempotency_enabled=idempotency_enabled,
    )

    steps = _load_trajectory(trajectory_file)
    effective_task_id = task_id or f"task-{trajectory_file.stem}"
    command = CommitCommand(
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        task_id=effective_task_id,
        trajectory=steps,
        labels={"task_type": task_type},
        is_incremental=False,
        trajectory_id=trajectory_id,
        visualize_graph_png=visualize_graph_png,
    )
    commit_result = orchestrator.commit(command)
    bundle = orchestrator.replay(commit_result.trajectory_id)
    if not bundle:
        raise RuntimeError("commit succeeded but replay lookup failed")

    base = Path(bundle["base_path"])
    return {
        "status": commit_result.status,
        "trajectory_id": commit_result.trajectory_id,
        "nodes": commit_result.nodes,
        "edges": commit_result.edges,
        "warnings": commit_result.warnings,
        "neo4j": commit_result.payload.get("neo4j_summary", {"enabled": False}),
        "vector_index": commit_result.payload.get("vector_index_summary", {"enabled": False}),
        "storage": {
            "base_path": str(base),
            "l0_abstract_path": str(base / ".abstract.md"),
            "l1_overview_path": str(base / ".overview.md"),
            "graph_pointer_path": str(base / "graph_pointer.json"),
            "raw_graph_path": bundle["graph_pointer"]["raw_graph_file"],
            "clean_graph_path": bundle["graph_pointer"]["clean_graph_file"],
            "trajectory_json_path": str(base / "trajectory.json"),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amc-commit-trajectory",
        description="Commit a trajectory JSON file and print L0/L1/graph storage locations.",
    )
    parser.add_argument("trajectory_file", help="Path to trajectory JSON (e.g. sample_traj/traj1.json)")
    parser.add_argument("--tenant-id", default="tenant-local", help="Tenant identifier")
    parser.add_argument("--agent-id", default="agent-local", help="Agent identifier")
    parser.add_argument("--session-id", default="session-local", help="Session identifier")
    parser.add_argument("--task-id", default=None, help="Task identifier (default: task-<filename>)")
    parser.add_argument("--task-type", default="sql_analysis", help="labels.task_type value")
    parser.add_argument("--trajectory-id", default=None, help="Optional explicit trajectory_id")
    parser.add_argument(
        "--visualize-graph-png",
        action="store_true",
        help="Generate raw_graph.png and clean_graph.png in trajectory directory (default: off).",
    )
    parser.add_argument(
        "--disable-idempotency",
        action="store_true",
        help="Disable idempotency for this run and force overwrite/update behavior.",
    )
    parser.add_argument("--config-path", default=None, help="Optional path to config YAML")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = run_commit(
        trajectory_file=Path(args.trajectory_file),
        tenant_id=args.tenant_id,
        agent_id=args.agent_id,
        session_id=args.session_id,
        task_id=args.task_id,
        task_type=args.task_type,
        trajectory_id=args.trajectory_id,
        visualize_graph_png=args.visualize_graph_png,
        disable_idempotency=args.disable_idempotency,
        config_path=args.config_path,
    )
    if args.pretty:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

