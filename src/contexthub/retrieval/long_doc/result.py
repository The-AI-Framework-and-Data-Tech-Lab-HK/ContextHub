from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

MAX_SNIPPET_CHARS = 4000


@dataclass(slots=True)
class LongDocRetrievalResult:
    context_id: UUID
    uri: str
    strategy: str
    section_id: int | None
    snippet: str
    snippet_offset: tuple[int, int] | None
    relevance_score: float

    def __post_init__(self) -> None:
        if self.strategy not in {"tree", "keyword"}:
            raise ValueError("strategy must be 'tree' or 'keyword'")
