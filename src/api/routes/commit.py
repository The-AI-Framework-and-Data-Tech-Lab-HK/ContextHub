"""Commit API route."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException

from api.deps import get_commit_orchestrator
from api.schemas.commit import (
    BatchCommitItemResponse,
    BatchCommitRequest,
    BatchCommitResponse,
    BatchCommitSummary,
    CommitRequest,
    CommitResponse,
)
from app.orchestrators.commit_orchestrator import CommitOrchestrator
from core.commit.batch_planner import plan_prepare_micro_batches
from core.commit.service import CommitCommand
from core.commit.validator import TrajectoryValidationError

router = APIRouter(tags=["commit"])


def _resolve_account_agent_context(
    *,
    body_account_id: str | None,
    body_agent_id: str | None,
    x_account_id: str | None,
    x_agent_id: str | None,
) -> tuple[str, str, list[str]]:
    resolved_account_id = (x_account_id or body_account_id or "").strip()
    if not resolved_account_id:
        raise HTTPException(
            status_code=422,
            detail="missing account context: provide X-Account-Id header or body.account_id",
        )
    resolved_agent_id = (x_agent_id or body_agent_id or "").strip()
    if not resolved_agent_id:
        raise HTTPException(
            status_code=422,
            detail="missing agent context: provide X-Agent-Id header or body.agent_id",
        )
    if x_account_id and body_account_id and body_account_id != x_account_id:
        raise HTTPException(status_code=422, detail="account context mismatch between header and body")
    if x_agent_id and body_agent_id and body_agent_id != x_agent_id:
        raise HTTPException(status_code=422, detail="agent context mismatch between header and body")
    deprecation_warnings: list[str] = []
    if body_agent_id:
        deprecation_warnings.append("body.agent_id is deprecated; use X-Agent-Id instead")
    return resolved_account_id, resolved_agent_id, deprecation_warnings


def _resolve_scope_owner_space(
    *,
    scope_raw: str | None,
    owner_space_raw: str | None,
    resolved_agent_id: str,
) -> tuple[str, str]:
    scope = (scope_raw or "agent").strip().lower() or "agent"
    if scope not in {"agent", "team", "datalake", "user"}:
        raise HTTPException(status_code=422, detail=f"invalid scope: {scope}")
    owner_space = (owner_space_raw or "").strip()
    if not owner_space:
        owner_space = resolved_agent_id if scope == "agent" else ""
    if scope == "agent" and owner_space != resolved_agent_id:
        raise HTTPException(
            status_code=422,
            detail="scope=agent requires owner_space to equal X-Agent-Id/body.agent_id",
        )
    if scope != "agent" and not owner_space:
        raise HTTPException(
            status_code=422,
            detail="owner_space is required when scope is not agent",
        )
    return scope, owner_space


@router.post("/commit", response_model=CommitResponse)
def commit_trajectory(
    body: CommitRequest,
    orchestrator: CommitOrchestrator = Depends(get_commit_orchestrator),
    x_account_id: str | None = Header(default=None, alias="X-Account-Id"),
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> CommitResponse:
    resolved_account_id, resolved_agent_id, deprecation_warnings = _resolve_account_agent_context(
        body_account_id=body.account_id,
        body_agent_id=body.agent_id,
        x_account_id=x_account_id,
        x_agent_id=x_agent_id,
    )
    scope, owner_space = _resolve_scope_owner_space(
        scope_raw=body.scope,
        owner_space_raw=body.owner_space,
        resolved_agent_id=resolved_agent_id,
    )
    try:
        result = orchestrator.commit(
            CommitCommand(
                agent_id=resolved_agent_id,
                account_id=resolved_account_id,
                scope=scope,
                owner_space=owner_space,
                session_id=body.session_id,
                task_id=body.task_id,
                trajectory=body.trajectory,
                labels=body.labels,
                is_incremental=body.is_incremental,
                trajectory_id=body.trajectory_id,
                visualize_graph_png=body.visualize_graph_png,
            )
        )
    except TrajectoryValidationError as e:
        # Keep validation errors explicit for client-side debugging.
        raise HTTPException(status_code=422, detail=str(e)) from e

    result.warnings.extend(deprecation_warnings)

    # Return Phase 1-compatible compact response.
    return CommitResponse(
        trajectory_id=result.trajectory_id,
        idempotency_key=result.idempotency_key,
        nodes=result.nodes,
        edges=result.edges,
        status=result.status,
        warnings=result.warnings,
        summary_l0=result.summary_l0,
        summary_l1=result.summary_l1,
        neo4j_summary=dict(result.payload.get("neo4j_summary") or {}),
        vector_index_summary=dict(result.payload.get("vector_index_summary") or {}),
    )


@router.post("/commit/batch", response_model=BatchCommitResponse)
def commit_trajectory_batch(
    body: BatchCommitRequest,
    orchestrator: CommitOrchestrator = Depends(get_commit_orchestrator),
    x_account_id: str | None = Header(default=None, alias="X-Account-Id"),
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> BatchCommitResponse:
    resolved_account_id, resolved_agent_id, deprecation_warnings = _resolve_account_agent_context(
        body_account_id=body.account_id,
        body_agent_id=body.agent_id,
        x_account_id=x_account_id,
        x_agent_id=x_agent_id,
    )
    scope, owner_space = _resolve_scope_owner_space(
        scope_raw=body.scope,
        owner_space_raw=body.owner_space,
        resolved_agent_id=resolved_agent_id,
    )
    if not body.items:
        raise HTTPException(status_code=422, detail="items must be non-empty")

    batch_id = (body.batch_id or "").strip() or f"batch_{uuid4().hex[:12]}"
    fail_fast = bool(body.options.fail_fast)
    item_results: list[BatchCommitItemResponse] = []
    commands = [
        CommitCommand(
            agent_id=resolved_agent_id,
            account_id=resolved_account_id,
            scope=scope,
            owner_space=owner_space,
            session_id=item.session_id,
            task_id=item.task_id,
            trajectory=item.trajectory,
            labels=item.labels,
            is_incremental=item.is_incremental,
            trajectory_id=item.trajectory_id,
            visualize_graph_png=item.visualize_graph_png,
        )
        for item in body.items
    ]

    if fail_fast:
        stop_reason: str | None = None
        for idx, command in enumerate(commands):
            item_id = str(idx)
            if stop_reason is not None:
                item_results.append(
                    BatchCommitItemResponse(
                        item_id=item_id,
                        status="skipped",
                        error_code="SKIPPED_FAIL_FAST",
                        error_message=stop_reason,
                    )
                )
                continue
            try:
                result = orchestrator.commit(command)
                item_results.append(
                    BatchCommitItemResponse(
                        item_id=item_id,
                        trajectory_id=result.trajectory_id,
                        idempotency_key=result.idempotency_key,
                        status=result.status,
                        nodes=result.nodes,
                        edges=result.edges,
                        warnings=list(result.warnings),
                        neo4j_summary=dict(result.payload.get("neo4j_summary") or {}),
                        vector_index_summary=dict(result.payload.get("vector_index_summary") or {}),
                    )
                )
            except TrajectoryValidationError as exc:
                item_results.append(
                    BatchCommitItemResponse(
                        item_id=item_id,
                        status="failed",
                        error_code="VALIDATION_ERROR",
                        error_message=str(exc),
                    )
                )
                stop_reason = f"fail_fast triggered at item {item_id}: {exc}"
            except Exception as exc:  # pragma: no cover - defensive guard
                item_results.append(
                    BatchCommitItemResponse(
                        item_id=item_id,
                        status="failed",
                        error_code=type(exc).__name__,
                        error_message=f"{type(exc).__name__}: {exc}",
                    )
                )
                stop_reason = f"fail_fast triggered at item {item_id}: {type(exc).__name__}"
    else:
        worker_hint = max(1, int(body.options.llm_batch_size_hint or 1))
        worker_cap = max(1, int(body.options.llm_max_items_per_batch or worker_hint))
        extractor_obj = (
            getattr(orchestrator.commit_service.dataflow_extractor, "__self__", None)
            if orchestrator.commit_service.dataflow_extractor
            else None
        )
        summarizer_obj = (
            getattr(orchestrator.commit_service.llm_summarizer, "__self__", None)
            if orchestrator.commit_service.llm_summarizer
            else None
        )
        llm_model = str(
            (getattr(extractor_obj, "model", None) or getattr(summarizer_obj, "model", None) or "")
        ).strip()
        llm_api_key = str(
            (getattr(extractor_obj, "api_key", None) or getattr(summarizer_obj, "api_key", None) or "")
        ).strip()
        llm_base_url = str(
            (getattr(extractor_obj, "base_url", None) or getattr(summarizer_obj, "base_url", None) or "")
        ).strip()
        planned_batches, _, _ = plan_prepare_micro_batches(
            commands,
            llm_token_usage_ratio=float(body.options.llm_token_usage_ratio),
            max_items_per_batch=worker_cap,
            max_context_tokens_fallback=int(body.options.llm_max_context_tokens_fallback),
            model=llm_model or None,
            api_key=llm_api_key or None,
            base_url=llm_base_url or None,
        )
        outcomes_by_idx: dict[int, Any] = {}
        for batch in planned_batches:
            group_indices = list(batch.indices)
            group_commands = [commands[i] for i in group_indices]
            llm_workers = max(1, min(len(group_commands), worker_hint, worker_cap))
            group_outcomes = orchestrator.prepare_commits(group_commands, max_workers=llm_workers)
            for local_idx, outcome in enumerate(group_outcomes):
                outcomes_by_idx[group_indices[local_idx]] = outcome

        prepared = [outcomes_by_idx[i] for i in range(len(commands))]
        for idx, outcome in enumerate(prepared):
            item_id = str(idx)
            if outcome.error is not None:
                if isinstance(outcome.error, TrajectoryValidationError):
                    item_results.append(
                        BatchCommitItemResponse(
                            item_id=item_id,
                            status="failed",
                            error_code="VALIDATION_ERROR",
                            error_message=str(outcome.error),
                        )
                    )
                else:
                    item_results.append(
                        BatchCommitItemResponse(
                            item_id=item_id,
                            status="failed",
                            error_code=type(outcome.error).__name__,
                            error_message=f"{type(outcome.error).__name__}: {outcome.error}",
                        )
                    )
                continue
            assert outcome.result is not None
            try:
                result = orchestrator.commit_prepared(outcome.command, outcome.result)
                item_results.append(
                    BatchCommitItemResponse(
                        item_id=item_id,
                        trajectory_id=result.trajectory_id,
                        idempotency_key=result.idempotency_key,
                        status=result.status,
                        nodes=result.nodes,
                        edges=result.edges,
                        warnings=list(result.warnings),
                        neo4j_summary=dict(result.payload.get("neo4j_summary") or {}),
                        vector_index_summary=dict(result.payload.get("vector_index_summary") or {}),
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                item_results.append(
                    BatchCommitItemResponse(
                        item_id=item_id,
                        status="failed",
                        error_code=type(exc).__name__,
                        error_message=f"{type(exc).__name__}: {exc}",
                    )
                )

    accepted = sum(1 for x in item_results if x.status == "accepted")
    idempotent = sum(1 for x in item_results if x.status == "idempotent")
    failed = sum(1 for x in item_results if x.status == "failed")
    skipped = sum(1 for x in item_results if x.status == "skipped")
    batch_status = "accepted" if failed == 0 and skipped == 0 else "accepted_partial"
    summary = BatchCommitSummary(
        total=len(body.items),
        accepted=accepted,
        idempotent=idempotent,
        failed=failed,
        skipped=skipped,
    )
    return BatchCommitResponse(
        batch_id=batch_id,
        status=batch_status,
        summary=summary,
        items=item_results,
        warnings=list(deprecation_warnings),
    )
