"""Tools API router: POST wrappers for Agent tool use."""

from dataclasses import asdict

from fastapi import APIRouter, Depends

from contexthub.api.deps import (
    get_acl_service,
    get_context_store,
    get_db,
    get_request_context,
    get_retrieval_service,
    get_skill_service,
)
from contexthub.db.repository import ScopedRepo
from contexthub.errors import ForbiddenError, NotFoundError
from contexthub.models.request import RequestContext
from contexthub.models.search import (
    SearchRequest,
    ToolGrepRequest,
    ToolLsRequest,
    ToolReadRequest,
    ToolStatRequest,
)
from contexthub.services.acl_service import ACLService
from contexthub.services.retrieval_service import RetrievalService
from contexthub.services.skill_service import SkillService
from contexthub.store.context_store import ContextStore

router = APIRouter(prefix="/api/v1", tags=["tools"])


@router.post("/tools/ls")
async def tool_ls(
    body: ToolLsRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    store: ContextStore = Depends(get_context_store),
):
    return await store.ls(db, body.path, ctx)


@router.post("/tools/read")
async def tool_read(
    body: ToolReadRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    store: ContextStore = Depends(get_context_store),
    acl: ACLService = Depends(get_acl_service),
    skill_svc: SkillService = Depends(get_skill_service),
):
    row = await db.fetchrow(
        "SELECT id, context_type FROM contexts WHERE uri = $1 AND status != 'deleted'",
        body.uri,
    )
    if row is None:
        raise NotFoundError(f"Context {body.uri} not found")

    if row["context_type"] == "skill":
        if not await acl.check_read(db, body.uri, ctx):
            raise ForbiddenError()
        result = await skill_svc.read_resolved(db, row["id"], ctx.agent_id, body.version)
        await db.execute(
            "UPDATE contexts SET last_accessed_at = NOW() WHERE uri = $1",
            body.uri,
        )
        return {
            "uri": body.uri,
            "version": result.version,
            "content": result.content,
            "status": result.status,
            "advisory": result.advisory,
        }

    content = await store.read(db, body.uri, body.level, ctx)
    return {"uri": body.uri, "level": body.level, "content": content}


@router.post("/tools/grep")
async def tool_grep(
    body: ToolGrepRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: RetrievalService = Depends(get_retrieval_service),
):
    request = SearchRequest(
        query=body.query,
        scope=body.scope,
        context_type=body.context_type,
        top_k=body.top_k,
    )
    return await svc.search(db, request, ctx)


@router.post("/tools/stat")
async def tool_stat(
    body: ToolStatRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    store: ContextStore = Depends(get_context_store),
):
    stat = await store.stat(db, body.uri, ctx)
    return asdict(stat)
