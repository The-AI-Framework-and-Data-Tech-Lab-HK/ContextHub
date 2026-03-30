"""Retrieve API route (Phase 2 semantic-only MVP)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException

from api.deps import get_retrieve_orchestrator
from api.schemas.retrieve import RetrieveRequest, RetrieveResponse
from app.orchestrators.retrieve_orchestrator import RetrieveOrchestrator
from core.retrieve.service import RetrieveCommand

router = APIRouter(tags=["retrieve"])


@router.post("/retrieve", response_model=RetrieveResponse)
def retrieve(
    body: RetrieveRequest,
    orchestrator: RetrieveOrchestrator = Depends(get_retrieve_orchestrator),
    x_account_id: str | None = Header(default=None, alias="X-Account-Id"),
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> RetrieveResponse:
    resolved_account_id = (x_account_id or body.tenant_id or "").strip()
    if not resolved_account_id:
        raise HTTPException(
            status_code=422,
            detail="missing account context: provide X-Account-Id header or body.tenant_id",
        )
    resolved_agent_id = (x_agent_id or body.agent_id or "").strip()
    if not resolved_agent_id:
        raise HTTPException(
            status_code=422,
            detail="missing agent context: provide X-Agent-Id header or body.agent_id",
        )
    if x_account_id and body.tenant_id and body.tenant_id != x_account_id:
        raise HTTPException(status_code=422, detail="account context mismatch between header and body")
    if x_agent_id and body.agent_id and body.agent_id != x_agent_id:
        raise HTTPException(status_code=422, detail="agent context mismatch between header and body")
    deprecation_warnings: list[str] = []
    if body.tenant_id:
        deprecation_warnings.append("body.tenant_id is deprecated; use X-Account-Id instead")
    if body.agent_id:
        deprecation_warnings.append("body.agent_id is deprecated; use X-Agent-Id instead")
    try:
        result = orchestrator.retrieve(
            RetrieveCommand(
                account_id=resolved_account_id,
                agent_id=resolved_agent_id,
                query=body.query.model_dump(),
                scope_filter=list(body.scope or []),
                owner_space_filter=list(body.owner_space or []),
                top_k=body.top_k,
                include_full_clean_graph=body.include_full_clean_graph,
            )
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"retrieve failed: {type(e).__name__}") from e
    return RetrieveResponse(items=result.items, warnings=[*result.warnings, *deprecation_warnings])
