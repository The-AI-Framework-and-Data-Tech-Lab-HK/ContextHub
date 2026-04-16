"""Promote API route for agent->team trajectory sharing."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException

from api.deps import get_promote_orchestrator
from api.schemas.promote import PromoteRequest, PromoteResponse
from app.orchestrators.promote_orchestrator import PromoteOrchestrator
from core.promote.service import PromoteCommand

router = APIRouter(tags=["promote"])


@router.post("/promote", response_model=PromoteResponse)
def promote_trajectory(
    body: PromoteRequest,
    orchestrator: PromoteOrchestrator = Depends(get_promote_orchestrator),
    x_account_id: str | None = Header(default=None, alias="X-Account-Id"),
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> PromoteResponse:
    account_id = (x_account_id or "").strip()
    agent_id = (x_agent_id or "").strip()
    if not account_id:
        raise HTTPException(status_code=422, detail="missing account context: provide X-Account-Id header")
    if not agent_id:
        raise HTTPException(status_code=422, detail="missing agent context: provide X-Agent-Id header")
    trajectory_id = str(body.trajectory_id or "").strip()
    target_team = str(body.target_team or "").strip()
    if not trajectory_id:
        raise HTTPException(status_code=422, detail="trajectory_id is required")
    if not target_team:
        raise HTTPException(status_code=422, detail="target_team is required")
    try:
        result = orchestrator.promote(
            PromoteCommand(
                account_id=account_id,
                agent_id=agent_id,
                trajectory_id=trajectory_id,
                target_team=target_team,
                reason=body.reason,
            )
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PromoteResponse(
        source_uri=result.source_uri,
        target_uri=result.target_uri,
        trajectory_id=result.trajectory_id,
        scope=result.scope,
        owner_space=result.owner_space,
        status=result.status,
        vector_index_summary=result.vector_index_summary,
    )
