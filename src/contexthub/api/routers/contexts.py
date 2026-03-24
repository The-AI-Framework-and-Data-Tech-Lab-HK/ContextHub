"""Context CRUD + store routes."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from contexthub.api.deps import (
    get_context_service,
    get_context_store,
    get_db,
    get_request_context,
)
from contexthub.db.repository import ScopedRepo
from contexthub.models.context import ContextLevel, CreateContextRequest, UpdateContextRequest
from contexthub.models.request import RequestContext
from contexthub.services.context_service import ContextService
from contexthub.store.context_store import ContextStore

router = APIRouter(prefix="/api/v1")


@router.post("/contexts", status_code=201)
async def create_context(
    body: CreateContextRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: ContextService = Depends(get_context_service),
):
    result = await svc.create(db, body, ctx)
    resp = JSONResponse(status_code=201, content=result.model_dump(mode="json"))
    resp.headers["ETag"] = str(result.version)
    return resp


@router.get("/contexts/{uri:path}/stat")
async def stat_context(
    uri: str,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    store: ContextStore = Depends(get_context_store),
):
    stat = await store.stat(db, uri, ctx)
    return asdict(stat)


@router.get("/contexts/{uri:path}/children")
async def list_children(
    uri: str,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    store: ContextStore = Depends(get_context_store),
):
    return await store.ls(db, uri, ctx)


@router.get("/contexts/{uri:path}/deps")
async def get_dependencies(
    uri: str,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: ContextService = Depends(get_context_service),
):
    return await svc.get_dependencies(db, uri, ctx)


@router.get("/contexts/{uri:path}")
async def read_context(
    uri: str,
    level: ContextLevel = Query(ContextLevel.L1),
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    store: ContextStore = Depends(get_context_store),
):
    content = await store.read(db, uri, level, ctx)
    return {"uri": uri, "level": level, "content": content}


@router.patch("/contexts/{uri:path}")
async def update_context(
    uri: str,
    body: UpdateContextRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: ContextService = Depends(get_context_service),
):
    result = await svc.update(db, uri, body, ctx)
    resp = JSONResponse(content=result.model_dump(mode="json"))
    resp.headers["ETag"] = str(result.version)
    return resp


@router.delete("/contexts/{uri:path}", status_code=204)
async def delete_context(
    uri: str,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: ContextService = Depends(get_context_service),
):
    await svc.delete(db, uri, ctx)
