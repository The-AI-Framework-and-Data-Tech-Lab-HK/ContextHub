"""Retrieve API route (Phase 2 semantic-only MVP)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_retrieve_orchestrator
from api.schemas.retrieve import RetrieveRequest, RetrieveResponse
from app.orchestrators.retrieve_orchestrator import RetrieveOrchestrator
from core.retrieve.service import RetrieveCommand

router = APIRouter(tags=["retrieve"])


@router.post("/retrieve", response_model=RetrieveResponse)
def retrieve(
    body: RetrieveRequest,
    orchestrator: RetrieveOrchestrator = Depends(get_retrieve_orchestrator),
) -> RetrieveResponse:
    try:
        result = orchestrator.retrieve(
            RetrieveCommand(
                tenant_id=body.tenant_id,
                agent_id=body.agent_id,
                query=body.query.model_dump(),
                top_k=body.top_k,
            )
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"retrieve failed: {type(e).__name__}") from e
    return RetrieveResponse(items=result.items, warnings=result.warnings)
