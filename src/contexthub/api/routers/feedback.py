"""Feedback API router."""

from fastapi import APIRouter, Depends

from contexthub.api.deps import get_db, get_feedback_service, get_request_context
from contexthub.db.repository import ScopedRepo
from contexthub.models.feedback import ContextFeedback, CreateFeedbackRequest
from contexthub.models.request import RequestContext
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
