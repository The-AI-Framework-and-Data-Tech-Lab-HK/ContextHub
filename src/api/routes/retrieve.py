"""Retrieve API route (Phase 2 semantic-only MVP)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

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
    result = orchestrator.retrieve(
        RetrieveCommand(
            tenant_id=body.tenant_id,
            agent_id=body.agent_id,
            query=body.query.model_dump(),
            top_k=body.top_k,
        )
    )
    return RetrieveResponse(items=result.items, warnings=result.warnings)
