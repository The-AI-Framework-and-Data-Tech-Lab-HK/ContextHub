"""Feedback API router."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query

from contexthub.api.deps import (
    get_acl_service,
    get_db,
    get_feedback_service,
    get_request_context,
)
from contexthub.db.repository import ScopedRepo
from contexthub.errors import BadRequestError, ForbiddenError, NotFoundError
from contexthub.models.feedback import ContextFeedback, CreateFeedbackRequest
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService
from contexthub.services.feedback_service import FeedbackService

router = APIRouter(prefix="/api/v1", tags=["feedback"])


@router.post("/feedback")
async def create_feedback(
    body: CreateFeedbackRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: FeedbackService = Depends(get_feedback_service),
) -> ContextFeedback:
    return await svc.record_feedback(
        db,
        body.context_uri,
        body.retrieval_id,
        body.outcome,
        ctx,
        body.metadata,
    )


@router.get("/feedback")
async def list_feedback(
    context_id: UUID | None = Query(None),
    retrieval_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: FeedbackService = Depends(get_feedback_service),
    acl: ACLService = Depends(get_acl_service),
) -> list[ContextFeedback]:
    """List feedback records filtered by context_id or retrieval_id.

    Pagination semantics: limit/offset apply to the raw SQL page. For
    retrieval_id queries the page is then silently filtered by ACL, so
    the response may contain fewer than `limit` records even when more
    visible records exist in later pages. Callers that need a precise
    page size should prefer context_id filtering (whose ACL check is
    performed up front) or drive pagination from a stable cursor.
    """
    if context_id is None and retrieval_id is None:
        raise BadRequestError("Either context_id or retrieval_id must be provided")

    if context_id is not None:
        context_row = await db.fetchrow(
            """
            SELECT id, uri
            FROM contexts
            WHERE id = $1 AND status != 'deleted'
            """,
            context_id,
        )
        if context_row is None:
            raise NotFoundError(f"Context {context_id} not found")

        decision = await acl.check_read_access(db, context_row["uri"], ctx)
        if not decision.allowed:
            raise ForbiddenError()

    records = await svc.list_feedback(
        db,
        context_id=context_id,
        retrieval_id=retrieval_id,
        limit=limit,
        offset=offset,
    )
    if not records or context_id is not None:
        return records

    visible_context_rows = await db.fetch(
        """
        SELECT id, uri, scope, owner_space, status
        FROM contexts
        WHERE id = ANY($1::uuid[]) AND status != 'deleted'
        """,
        list({record.context_id for record in records}),
    )
    visible_context_ids = {
        row["id"]
        for row, _masks in await acl.filter_visible_with_acl(db, visible_context_rows, ctx)
    }
    return [record for record in records if record.context_id in visible_context_ids]
