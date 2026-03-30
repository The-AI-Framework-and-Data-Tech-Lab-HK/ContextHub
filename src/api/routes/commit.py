"""Commit API route."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException

from api.deps import get_commit_orchestrator
from api.schemas.commit import CommitRequest, CommitResponse
from app.orchestrators.commit_orchestrator import CommitOrchestrator
from core.commit.service import CommitCommand
from core.commit.validator import TrajectoryValidationError

router = APIRouter(tags=["commit"])


@router.post("/commit", response_model=CommitResponse)
def commit_trajectory(
    body: CommitRequest,
    orchestrator: CommitOrchestrator = Depends(get_commit_orchestrator),
    x_account_id: str | None = Header(default=None, alias="X-Account-Id"),
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> CommitResponse:
    # Map HTTP payload to internal command object.
    resolved_account_id = (x_account_id or body.account_id or body.tenant_id or "").strip()
    if not resolved_account_id:
        raise HTTPException(
            status_code=422,
            detail="missing account context: provide X-Account-Id header or body.account_id/tenant_id",
        )
    resolved_agent_id = (x_agent_id or body.agent_id or "").strip()
    if not resolved_agent_id:
        raise HTTPException(
            status_code=422,
            detail="missing agent context: provide X-Agent-Id header or body.agent_id",
        )
    if x_account_id and body.account_id and body.account_id != x_account_id:
        raise HTTPException(status_code=422, detail="account context mismatch between header and body")
    if x_agent_id and body.agent_id and body.agent_id != x_agent_id:
        raise HTTPException(status_code=422, detail="agent context mismatch between header and body")
    scope = (body.scope or "agent").strip().lower() or "agent"
    if scope not in {"agent", "team", "datalake", "user"}:
        raise HTTPException(status_code=422, detail=f"invalid scope: {scope}")
    owner_space = (body.owner_space or "").strip()
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
    deprecation_warnings: list[str] = []
    if body.tenant_id:
        deprecation_warnings.append(
            "body.tenant_id is deprecated; use X-Account-Id (or body.account_id) instead"
        )
    if body.agent_id:
        deprecation_warnings.append("body.agent_id is deprecated; use X-Agent-Id instead")
    try:
        result = orchestrator.commit(
            CommitCommand(
                tenant_id=body.tenant_id or resolved_account_id,
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
