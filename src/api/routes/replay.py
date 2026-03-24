"""Replay API route."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_commit_orchestrator
from api.schemas.commit import ReplayResponse
from app.orchestrators.commit_orchestrator import CommitOrchestrator

router = APIRouter(tags=["replay"])


@router.get("/replay/{trajectory_id}", response_model=ReplayResponse)
def replay_trajectory(
    trajectory_id: str,
    orchestrator: CommitOrchestrator = Depends(get_commit_orchestrator),
) -> ReplayResponse:
    # Replay returns stored artifacts from local FS repository.
    bundle = orchestrator.replay(trajectory_id)
    if not bundle:
        raise HTTPException(status_code=404, detail="trajectory not found")
    return ReplayResponse(
        trajectory_id=trajectory_id,
        meta=bundle["meta"],
        trajectory=bundle["trajectory"],
        graph_pointer=bundle["graph_pointer"],
        abstract=bundle["abstract"],
        overview=bundle["overview"],
    )
