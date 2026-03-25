"""Search API router."""

from fastapi import APIRouter, Depends

from contexthub.api.deps import get_db, get_request_context, get_retrieval_service
from contexthub.db.repository import ScopedRepo
from contexthub.models.request import RequestContext
from contexthub.models.search import SearchRequest, SearchResponse
from contexthub.services.retrieval_service import RetrievalService

router = APIRouter(prefix="/api/v1", tags=["search"])


@router.post("/search")
async def search(
    body: SearchRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: RetrievalService = Depends(get_retrieval_service),
) -> SearchResponse:
    return await svc.search(db, body, ctx)
