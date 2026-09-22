"""Memory API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from contexthub.api.deps import get_db, get_memory_service, get_request_context
from contexthub.db.repository import ScopedRepo
from contexthub.models.memory import AddMemoryRequest, PromoteRequest
from contexthub.models.request import RequestContext
from contexthub.services.memory_service import MemoryService

router = APIRouter(prefix="/api/v1")


@router.post("/memories", status_code=201)
async def add_memory(
    body: AddMemoryRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: MemoryService = Depends(get_memory_service),
):
    result = await svc.add_memory(db, body, ctx)
    return result.model_dump(mode="json")


@router.get("/memories")
async def list_memories(
    include_stale: bool = Query(False),
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: MemoryService = Depends(get_memory_service),
):
    return await svc.list_memories(db, ctx, include_stale=include_stale)


@router.post("/memories/promote", status_code=201)
async def promote_memory(
    body: PromoteRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: MemoryService = Depends(get_memory_service),
):
    result = await svc.promote(db, body, ctx)
    return result.model_dump(mode="json")
