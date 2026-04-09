"""Context CRUD + store routes."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from contexthub.api.deps import (
    get_acl_service,
    get_context_service,
    get_context_store,
    get_db,
    get_masking_service,
    get_request_context,
    get_skill_service,
)
from contexthub.db.repository import ScopedRepo
from contexthub.errors import BadRequestError, ForbiddenError, NotFoundError
from contexthub.models.context import ContextLevel, CreateContextRequest, UpdateContextRequest
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService
from contexthub.services.context_service import ContextService
from contexthub.services.masking_service import MaskingService
from contexthub.services.skill_service import SkillService
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
    version: int | None = Query(None),
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    store: ContextStore = Depends(get_context_store),
    acl: ACLService = Depends(get_acl_service),
    skill_svc: SkillService = Depends(get_skill_service),
    masking: MaskingService = Depends(get_masking_service),
):
    _ensure_supported_public_uri(uri)

    # Check if this is a skill context
    row = await db.fetchrow(
        "SELECT id, context_type FROM contexts WHERE uri = $1 AND status != 'deleted'",
        uri,
    )
    if row is None:
        raise NotFoundError(f"Context {uri} not found")

    if row["context_type"] == "skill":
        decision = await acl.check_read_access(db, uri, ctx)
        if not decision.allowed:
            raise ForbiddenError()
        result = await skill_svc.read_resolved(db, row["id"], ctx.agent_id, version)
        await db.execute(
            "UPDATE contexts SET last_accessed_at = NOW() WHERE uri = $1",
            uri,
        )
        content = result.content
        if decision.field_masks:
            content = masking.apply_masks(content, decision.field_masks)
        return {
            "uri": uri,
            "version": result.version,
            "content": content,
            "status": result.status,
            "advisory": result.advisory,
        }

    # Non-skill: original path
    content = await store.read(db, uri, level, ctx)
    return {"uri": uri, "level": level, "content": content}


@router.patch("/contexts/{uri:path}")
async def update_context(
    uri: str,
    body: UpdateContextRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: ContextService = Depends(get_context_service),
    acl: ACLService = Depends(get_acl_service),
):
    row = await db.fetchrow(
        "SELECT context_type FROM contexts WHERE uri = $1 AND status != 'deleted'",
        uri,
    )
    if row is not None and row["context_type"] == "skill":
        if not await acl.check_write(db, uri, ctx):
            raise ForbiddenError()
        raise BadRequestError("Skills are immutable via PATCH; use POST /api/v1/skills/versions")

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


def _ensure_supported_public_uri(uri: str) -> None:
    if uri.startswith("ctx://user/"):
        raise BadRequestError("scope=user is not supported in Task 2 public API")
