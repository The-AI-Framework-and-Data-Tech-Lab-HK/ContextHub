"""Commit API route."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

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
) -> CommitResponse:
    # Map HTTP payload to internal command object.
    try:
        result = orchestrator.commit(
            CommitCommand(
                tenant_id=body.tenant_id,
                agent_id=body.agent_id,
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

    # Return Phase 1-compatible compact response.
    return CommitResponse(
        trajectory_id=result.trajectory_id,
        nodes=result.nodes,
        edges=result.edges,
        status=result.status,
        warnings=result.warnings,
    )
