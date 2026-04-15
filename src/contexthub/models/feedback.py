from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel


class FeedbackOutcome(StrEnum):
    ADOPTED = "adopted"
    IGNORED = "ignored"
    CORRECTED = "corrected"
    IRRELEVANT = "irrelevant"


class ContextFeedback(BaseModel):
    """对应 context_feedback 表的完整模型。"""

    id: int
    context_id: UUID
    retrieval_id: str
    actor: str
    retrieved_at: datetime | None = None
    outcome: FeedbackOutcome
    metadata: dict | None = None
    account_id: str
    created_at: datetime | None = None


class CreateFeedbackRequest(BaseModel):
    """创建反馈的请求体（API 层使用）。"""

    context_uri: str
    outcome: FeedbackOutcome
    retrieval_id: str | None = None
    metadata: dict | None = None


class QualityReportItem(BaseModel):
    """低质量上下文报告中的单条记录。"""

    context_id: UUID
    uri: str
    context_type: str
    scope: str
    active_count: int
    adopted_count: int
    ignored_count: int
    adoption_rate: float
    quality_score: float


class QualityReport(BaseModel):
    """低质量上下文报告（Admin API 响应）。"""

    items: list[QualityReportItem]
    total: int
    min_active_count: int
    max_adoption_rate: float
