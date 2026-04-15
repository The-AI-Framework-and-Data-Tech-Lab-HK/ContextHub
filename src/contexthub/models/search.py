"""Search and tools request/response models."""

from pydantic import BaseModel, Field

from contexthub.models.context import ContextLevel, ContextType, Scope


class SearchRequest(BaseModel):
    query: str
    scope: list[Scope] | None = None
    context_type: list[ContextType] | None = None
    top_k: int = Field(default=10, ge=1, le=100)
    level: ContextLevel = ContextLevel.L1
    include_stale: bool = True


class SearchResult(BaseModel):
    uri: str
    context_type: str
    scope: str
    owner_space: str | None = None
    score: float
    l0_content: str | None = None
    l1_content: str | None = None
    l2_content: str | None = None
    status: str
    version: int
    tags: list[str] = Field(default_factory=list)
    snippet: str | None = None
    section_id: int | None = None
    retrieval_strategy: str | None = None


class SearchResponse(BaseModel):
    results: list[SearchResult]
    total: int
    retrieval_id: str = ""


class ToolLsRequest(BaseModel):
    path: str


class ToolReadRequest(BaseModel):
    uri: str
    level: ContextLevel = ContextLevel.L1
    version: int | None = None


class ToolGrepRequest(BaseModel):
    query: str
    scope: list[Scope] | None = None
    context_type: list[ContextType] | None = None
    top_k: int = Field(default=5, ge=1, le=50)


class ToolStatRequest(BaseModel):
    uri: str
