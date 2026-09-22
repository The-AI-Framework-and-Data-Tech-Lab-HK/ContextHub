"""Search and tools request/response models."""

from pydantic import BaseModel, Field

from contexthub.models.context import ContextLevel, ContextType, Scope


class SearchRequest(BaseModel):
    query: str
    scope: list[Scope] | None = None
    context_type: list[ContextType] | None = None
    top_k: int = Field(default=10, ge=1, le=100)
    level: ContextLevel = ContextLevel.L1
    # Unsafe/debug reads are explicit. Normal retrieval serves only fresh,
    # current versions.
    include_stale: bool = False
    # Explain what was withheld instead of silently dropping it. Withheld nodes
    # never enter ``results``; they are reported separately as notices, so the
    # caller can say "this is no longer reliable" instead of showing a hole.
    # Has no effect when ``include_stale`` is set (then nothing is withheld).
    include_stale_notices: bool = False


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
    validity_status: str = "fresh"
    tags: list[str] = Field(default_factory=list)
    snippet: str | None = None
    section_id: int | None = None
    retrieval_strategy: str | None = None


class StaleNotice(BaseModel):
    """A node that was withheld because it is no longer valid, plus why.

    ``stale_content`` is the withheld node's own body — the value that *used* to
    be served.  It is reported as a superseded prior value, never as a servable
    result: it stays out of ``SearchResponse.results`` and is only ever rendered
    behind an explicit "no longer reliable" framing.
    """

    uri: str
    validity_status: str
    reason: str | None = None
    stale_content: str | None = None
    version: int | None = None
    # The nearest upstream node whose change invalidated this one: for a
    # multi-hop closure this is the immediate predecessor, not the original
    # root.  Absent when no unresolved invalidation names an upstream, or when
    # the caller cannot read that upstream.
    source_uri: str | None = None
    source_content: str | None = None


class SearchResponse(BaseModel):
    results: list[SearchResult]
    total: int
    retrieval_id: str = Field(min_length=1)
    trace: dict = Field(default_factory=dict)
    # Populated only when the request set ``include_stale_notices``.
    stale_notices: list[StaleNotice] = Field(default_factory=list)


class ToolLsRequest(BaseModel):
    path: str
    include_stale: bool = False


class ToolReadRequest(BaseModel):
    uri: str
    level: ContextLevel = ContextLevel.L1
    version: int | None = None
    include_stale: bool = False


class ToolGrepRequest(BaseModel):
    query: str
    scope: list[Scope] | None = None
    context_type: list[ContextType] | None = None
    top_k: int = Field(default=5, ge=1, le=50)


class ToolStatRequest(BaseModel):
    uri: str
