"""Commit a batch of trajectories from command line (defaults to Alfworld 0001-0008)."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import load_settings
from app.orchestrators.commit_orchestrator import CommitOrchestrator, PreparedCommitOutcome
from core.commit.batch_planner import plan_prepare_micro_batches
from core.commit.dataflow_llm import LLMDataflowExtractor
from core.commit.service import CommitCommand, CommitService
from core.commit.summary_llm import LLMTrajectorySummarizer
from core.commit.validator import TrajectoryValidationError
from core.indexing.trajectory_vector_indexer import TrajectoryVectorIndexer
from infra.audit.audit_logger import JsonlAuditLogger
from infra.storage.fs.trajectory_repo import LocalFSTrajectoryRepository
from infra.storage.graph.factory import build_graph_store_writer
from infra.storage.vector.factory import build_vector_store_adapter


@dataclass
class _RuntimeBundle:
    orchestrator: CommitOrchestrator
    extractor_obj: LLMDataflowExtractor | None
    summarizer_obj: LLMTrajectorySummarizer | None


def _load_trajectory(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"trajectory file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get("trajectory")
        if isinstance(inner, list):
            return inner
    raise ValueError(
        f"trajectory JSON must be a list or an object with a 'trajectory' field: {path}"
    )


def _default_alfworld_paths(base_dir: Path, *, start: int, end: int) -> list[Path]:
    if end < start:
        raise ValueError(f"invalid alfworld index range: start={start}, end={end}")
    paths = [base_dir / f"traj_alfworld_{i:04d}.json" for i in range(start, end + 1)]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "default alfworld batch files missing: "
            + ", ".join(missing)
            + " (did you run under repository root?)"
        )
    return paths


def _build_runtime(*, config_path: str | None, disable_idempotency: bool) -> _RuntimeBundle:
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

    extractor_obj: LLMDataflowExtractor | None = None
    summarizer_obj: LLMTrajectorySummarizer | None = None
    llm_summarizer = None
    dataflow_extractor = None
    if settings.openai_api_key:
        summarizer_obj = LLMTrajectorySummarizer(
            api_key=settings.openai_api_key,
            model=settings.llm_model,
            base_url=settings.model_endpoints.llm_base_url or None,
            temperature=0.0,
        )
        llm_summarizer = summarizer_obj.summarize
    if settings.commit.dataflow_extractor.lower() == "llm":
        if settings.openai_api_key:
            extractor_obj = LLMDataflowExtractor(
                api_key=settings.openai_api_key,
                model=settings.llm_model,
                base_url=settings.model_endpoints.llm_base_url or None,
                temperature=settings.commit.dataflow_llm_temperature,
            )
            dataflow_extractor = extractor_obj.extract
        else:
            print("[AMC] dataflow_extractor=llm but AMC_OPENAI_API_KEY is empty, fallback to rule_based.")

    service = CommitService(
        max_action_result_chars=settings.commit.max_action_result_chars,
        temporal_fallback_edge=settings.commit.temporal_fallback_edge,
        dataflow_extractor=dataflow_extractor,
        llm_summarizer=llm_summarizer,
        reasoning_min_confidence=settings.commit.reasoning_min_confidence,
    )
    idempotency_enabled = False if disable_idempotency else settings.commit.idempotency_enabled
    orchestrator = CommitOrchestrator(
        commit_service=service,
        repo=repo,
        audit=audit,
        graph_store=graph_store,
        vector_indexer=vector_indexer,
        idempotency_enabled=idempotency_enabled,
    )
    return _RuntimeBundle(
        orchestrator=orchestrator,
        extractor_obj=extractor_obj,
        summarizer_obj=summarizer_obj,
    )


def _storage_paths(orchestrator: CommitOrchestrator, trajectory_id: str) -> dict[str, Any]:
    bundle = orchestrator.replay(trajectory_id)
    if not bundle:
        return {}
    base = Path(bundle["base_path"])
    return {
        "base_path": str(base),
        "l0_abstract_path": str(base / ".abstract.md"),
        "l1_overview_path": str(base / ".overview.md"),
        "graph_pointer_path": str(base / "graph_pointer.json"),
        "raw_graph_path": bundle["graph_pointer"]["raw_graph_file"],
        "clean_graph_path": bundle["graph_pointer"]["clean_graph_file"],
        "trajectory_json_path": str(base / "trajectory.json"),
    }


def _failed_item(
    item_id: str,
    source_file: str,
    code: str,
    message: str,
    *,
    extraction_success: bool | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "item_id": item_id,
        "source_file": source_file,
        "status": "failed",
        "extraction_success": extraction_success,
        "error_code": code,
        "error_message": message,
    }
    return payload


def _is_extraction_success(result: Any) -> bool:
    traces = result.payload.get("llm_extraction_traces")
    if not isinstance(traces, list) or not traces:
        # Rule-based path or no trace export: treat prepare stage as successful.
        return True
    for t in traces:
        if not isinstance(t, dict):
            continue
        if str(t.get("error") or "").strip():
            return False
    return True


def _result_item(
    *,
    item_id: str,
    source_file: str,
    result: Any,
    orchestrator: CommitOrchestrator,
    output_mode: str,
) -> dict[str, Any]:
    extraction_success = _is_extraction_success(result)
    compact = {
        "item_id": item_id,
        "source_file": source_file,
        "status": result.status,
        "trajectory_id": result.trajectory_id,
        "extraction_success": extraction_success,
    }
    if output_mode != "full":
        return compact
    compact.update(
        {
            "idempotency_key": result.idempotency_key,
            "nodes": result.nodes,
            "edges": result.edges,
            "warnings": list(result.warnings),
            "neo4j_summary": dict(result.payload.get("neo4j_summary") or {}),
            "vector_index_summary": dict(result.payload.get("vector_index_summary") or {}),
            "storage": _storage_paths(orchestrator, result.trajectory_id),
        }
    )
    return compact


def _resolve_paths(
    *,
    trajectory_files: list[str],
    alfworld_dir: str,
    range_start: int,
    range_end: int,
) -> list[Path]:
    if trajectory_files:
        return [Path(x) for x in trajectory_files]
    return _default_alfworld_paths(
        Path(alfworld_dir),
        start=range_start,
        end=range_end,
    )


def run_commit_batch(
    *,
    trajectory_files: list[str],
    alfworld_dir: str,
    range_start: int,
    range_end: int,
    account_id: str,
    agent_id: str,
    scope: str,
    owner_space: str | None,
    session_prefix: str,
    task_prefix: str,
    labels: dict[str, Any],
    fail_fast: bool,
    llm_batch_size_hint: int,
    llm_max_items_per_batch: int,
    llm_token_usage_ratio: float,
    llm_max_context_tokens_fallback: int,
    visualize_graph_png: bool,
    disable_idempotency: bool,
    config_path: str | None,
    output_mode: str = "compact",
) -> dict[str, Any]:
    t0 = time.perf_counter()
    resolved_paths = _resolve_paths(
        trajectory_files=trajectory_files,
        alfworld_dir=alfworld_dir,
        range_start=range_start,
        range_end=range_end,
    )
    runtime = _build_runtime(config_path=config_path, disable_idempotency=disable_idempotency)
    orchestrator = runtime.orchestrator
    resolved_account_id = str(account_id or "account-local").strip()

    commands: list[CommitCommand] = []
    source_by_idx: dict[int, str] = {}
    for idx, path in enumerate(resolved_paths, 1):
        steps = _load_trajectory(path)
        source_by_idx[idx - 1] = str(path)
        commands.append(
            CommitCommand(
                agent_id=agent_id,
                account_id=resolved_account_id,
                scope=scope,
                owner_space=owner_space,
                session_id=f"{session_prefix}-{idx:04d}",
                task_id=f"{task_prefix}-{idx:04d}-{path.stem}",
                trajectory=steps,
                labels=dict(labels),
                is_incremental=False,
                trajectory_id=None,
                visualize_graph_png=visualize_graph_png,
            )
        )
    t1 = time.perf_counter()
    load_inputs_seconds = t1 - t0

    batch_id = f"cli_batch_{uuid4().hex[:12]}"
    items: list[dict[str, Any]] = []
    prepare_seconds = 0.0
    persist_seconds = 0.0
    if fail_fast:
        stop_reason: str | None = None
        for idx, command in enumerate(commands):
            item_id = str(idx)
            source = source_by_idx[idx]
            if stop_reason is not None:
                items.append(
                    {
                        "item_id": item_id,
                        "source_file": source,
                        "status": "skipped",
                        "extraction_success": None,
                        "error_code": "SKIPPED_FAIL_FAST",
                        "error_message": stop_reason,
                    }
                )
                continue
            try:
                t_prepare = time.perf_counter()
                prepared = orchestrator.prepare_commit(command)
                prepare_seconds += time.perf_counter() - t_prepare
            except TrajectoryValidationError as exc:
                items.append(
                    _failed_item(
                        item_id=item_id,
                        source_file=source,
                        code="VALIDATION_ERROR",
                        message=str(exc),
                        extraction_success=False,
                    )
                )
                stop_reason = f"fail_fast triggered at item {item_id}: {exc}"
                continue
            except Exception as exc:  # pragma: no cover - defensive guard
                items.append(
                    _failed_item(
                        item_id=item_id,
                        source_file=source,
                        code=type(exc).__name__,
                        message=f"{type(exc).__name__}: {exc}",
                        extraction_success=False,
                    )
                )
                stop_reason = f"fail_fast triggered at item {item_id}: {type(exc).__name__}"
                continue
            try:
                t_persist = time.perf_counter()
                result = orchestrator.commit_prepared(command, prepared)
                persist_seconds += time.perf_counter() - t_persist
                items.append(
                    _result_item(
                        item_id=item_id,
                        source_file=source,
                        result=result,
                        orchestrator=orchestrator,
                        output_mode=output_mode,
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                items.append(
                    _failed_item(
                        item_id=item_id,
                        source_file=source,
                        code=type(exc).__name__,
                        message=f"{type(exc).__name__}: {exc}",
                        extraction_success=True,
                    )
                )
                stop_reason = f"fail_fast triggered at item {item_id}: {type(exc).__name__}"
    else:
        worker_hint = max(1, int(llm_batch_size_hint or 1))
        worker_cap = max(1, int(llm_max_items_per_batch or worker_hint))
        llm_model = str(
            (getattr(runtime.extractor_obj, "model", None) or getattr(runtime.summarizer_obj, "model", None) or "")
        ).strip()
        llm_api_key = str(
            (getattr(runtime.extractor_obj, "api_key", None) or getattr(runtime.summarizer_obj, "api_key", None) or "")
        ).strip()
        llm_base_url = str(
            (
                getattr(runtime.extractor_obj, "base_url", None)
                or getattr(runtime.summarizer_obj, "base_url", None)
                or ""
            )
        ).strip()
        planned_batches, _, _ = plan_prepare_micro_batches(
            commands,
            llm_token_usage_ratio=float(llm_token_usage_ratio),
            max_items_per_batch=worker_cap,
            max_context_tokens_fallback=int(llm_max_context_tokens_fallback),
            model=llm_model or None,
            api_key=llm_api_key or None,
            base_url=llm_base_url or None,
        )
        outcomes_by_idx: dict[int, PreparedCommitOutcome] = {}
        for batch in planned_batches:
            group_indices = list(batch.indices)
            group_commands = [commands[i] for i in group_indices]
            llm_workers = max(1, min(len(group_commands), worker_hint, worker_cap))
            t_prepare_group = time.perf_counter()
            group_outcomes = orchestrator.prepare_commits(group_commands, max_workers=llm_workers)
            prepare_seconds += time.perf_counter() - t_prepare_group
            for local_idx, outcome in enumerate(group_outcomes):
                outcomes_by_idx[group_indices[local_idx]] = outcome

        prepared = [outcomes_by_idx[i] for i in range(len(commands))]
        for idx, outcome in enumerate(prepared):
            item_id = str(idx)
            source = source_by_idx[idx]
            if outcome.error is not None:
                if isinstance(outcome.error, TrajectoryValidationError):
                    items.append(
                        _failed_item(
                            item_id=item_id,
                            source_file=source,
                            code="VALIDATION_ERROR",
                            message=str(outcome.error),
                            extraction_success=False,
                        )
                    )
                else:
                    items.append(
                        _failed_item(
                            item_id=item_id,
                            source_file=source,
                            code=type(outcome.error).__name__,
                            message=f"{type(outcome.error).__name__}: {outcome.error}",
                            extraction_success=False,
                        )
                    )
                continue
            assert outcome.result is not None
            try:
                t_persist = time.perf_counter()
                result = orchestrator.commit_prepared(outcome.command, outcome.result)
                persist_seconds += time.perf_counter() - t_persist
                items.append(
                    _result_item(
                        item_id=item_id,
                        source_file=source,
                        result=result,
                        orchestrator=orchestrator,
                        output_mode=output_mode,
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                items.append(
                    _failed_item(
                        item_id=item_id,
                        source_file=source,
                        code=type(exc).__name__,
                        message=f"{type(exc).__name__}: {exc}",
                        extraction_success=True,
                    )
                )

    accepted = sum(1 for x in items if x.get("status") == "accepted")
    idempotent = sum(1 for x in items if x.get("status") == "idempotent")
    failed = sum(1 for x in items if x.get("status") == "failed")
    skipped = sum(1 for x in items if x.get("status") == "skipped")
    batch_status = "accepted" if failed == 0 and skipped == 0 else "accepted_partial"
    extraction_success_count = sum(1 for x in items if x.get("extraction_success") is True)
    total_seconds = time.perf_counter() - t0
    result = {
        "batch_id": batch_id,
        "status": batch_status,
        "summary": {
            "total": len(commands),
            "accepted": accepted,
            "idempotent": idempotent,
            "failed": failed,
            "skipped": skipped,
            "extraction_success": extraction_success_count,
        },
        "items": items,
        "timing": {
            "load_inputs_seconds": round(load_inputs_seconds, 3),
            "prepare_seconds": round(prepare_seconds, 3),
            "persist_seconds": round(persist_seconds, 3),
            "total_seconds": round(total_seconds, 3),
        },
        "resolved_inputs": [str(p) for p in resolved_paths],
    }
    if output_mode != "full":
        # Compact output only keeps essential success/error signal per trajectory.
        result.pop("resolved_inputs", None)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amc-commit-trajectory-batch",
        description=(
            "Commit trajectory batch in-process. "
            "Default behavior commits Alfworld traj_0001~0008 from sample_traj/alfworld/data."
        ),
    )
    parser.add_argument(
        "trajectory_files",
        nargs="*",
        help=(
            "Optional explicit trajectory files. "
            "If empty, command commits Alfworld traj_0001~0008 by default."
        ),
    )
    parser.add_argument("--alfworld-dir", default="sample_traj/alfworld/data")
    parser.add_argument("--range-start", type=int, default=1)
    parser.add_argument("--range-end", type=int, default=8)
    parser.add_argument("--account-id", default="account-local", help="Account identifier (primary)")
    parser.add_argument("--agent-id", default="agent-local", help="Agent identifier")
    parser.add_argument(
        "--scope",
        default="agent",
        choices=["agent", "team", "datalake", "user"],
        help="Visibility scope used for storage hierarchy and metadata.",
    )
    parser.add_argument("--owner-space", default=None, help="Owner space id under scope.")
    parser.add_argument("--session-prefix", default="alfworld-batch-session")
    parser.add_argument("--task-prefix", default="alfworld-batch-task")
    parser.add_argument(
        "--labels-json",
        default="{}",
        help='Extra labels JSON applied to each item (e.g. \'{"source":"cli"}\').',
    )
    parser.add_argument("--fail-fast", action="store_true", help="Stop processing remaining items on first failure.")
    parser.add_argument("--llm-batch-size-hint", type=int, default=8)
    parser.add_argument("--llm-max-items-per-batch", type=int, default=16)
    parser.add_argument("--llm-token-usage-ratio", type=float, default=0.6)
    parser.add_argument("--llm-max-context-tokens-fallback", type=int, default=24000)
    parser.add_argument("--visualize-graph-png", action="store_true")
    parser.add_argument(
        "--disable-idempotency",
        action="store_true",
        help="Disable idempotency for this run and force overwrite/update behavior.",
    )
    parser.add_argument(
        "--output-mode",
        choices=["compact", "full"],
        default="compact",
        help="compact: only status/extraction + timing; full: include detailed storage summaries.",
    )
    parser.add_argument("--config-path", default=None, help="Optional path to config YAML")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    labels = json.loads(args.labels_json)
    if not isinstance(labels, dict):
        raise ValueError("labels-json must decode to a JSON object")
    result = run_commit_batch(
        trajectory_files=list(args.trajectory_files),
        alfworld_dir=args.alfworld_dir,
        range_start=args.range_start,
        range_end=args.range_end,
        account_id=args.account_id,
        agent_id=args.agent_id,
        scope=args.scope,
        owner_space=args.owner_space,
        session_prefix=args.session_prefix,
        task_prefix=args.task_prefix,
        labels=labels,
        fail_fast=args.fail_fast,
        llm_batch_size_hint=args.llm_batch_size_hint,
        llm_max_items_per_batch=args.llm_max_items_per_batch,
        llm_token_usage_ratio=args.llm_token_usage_ratio,
        llm_max_context_tokens_fallback=args.llm_max_context_tokens_fallback,
        visualize_graph_png=args.visualize_graph_png,
        disable_idempotency=args.disable_idempotency,
        config_path=args.config_path,
        output_mode=args.output_mode,
    )
    if args.pretty:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

