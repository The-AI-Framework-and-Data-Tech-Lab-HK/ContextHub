from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class DocumentSection(BaseModel):
    """对应 document_sections 表的完整模型。"""

    section_id: int
    context_id: UUID
    parent_id: int | None = None
    node_id: str
    title: str
    depth: int = 0
    start_offset: int | None = None
    end_offset: int | None = None
    summary: str | None = None
    token_count: int | None = None
    account_id: str
    created_at: datetime | None = None


class SectionNode(BaseModel):
    """树构建中间表示——用于 LongDocumentIngester 内部。"""

    node_id: str
    parent_node_id: str | None = None
    title: str
    depth: int = 0
    start_offset: int | None = None
    end_offset: int | None = None
    summary: str | None = None
    token_count: int | None = None
    children: list["SectionNode"] = Field(default_factory=list)


class DocumentIngestRequest(BaseModel):
    """长文档入库请求体。"""

    uri: str
    tags: list[str] | None = None


class DocumentIngestResponse(BaseModel):
    """长文档入库响应。"""

    context_id: UUID
    uri: str
    section_count: int
    file_path: str


SectionNode.model_rebuild()
