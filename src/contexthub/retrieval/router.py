"""RetrievalRouter: strategy dispatch for retrieval and reranking."""

from __future__ import annotations

from contexthub.retrieval.rerank import KeywordRerankStrategy, RerankStrategy


class RetrievalRouter:
    """Strategy dispatcher: decides which retrieval and rerank strategy to use."""

    def __init__(self, rerank_strategy: RerankStrategy | None, *, vector_only_tables: bool = False):
        self._rerank = rerank_strategy
        self._vector_only_tables = vector_only_tables

    @classmethod
    def default(cls, *, vector_only_tables: bool = False) -> RetrievalRouter:
        return cls(rerank_strategy=KeywordRerankStrategy(), vector_only_tables=vector_only_tables)

    def for_context_types(self, context_types: list[str] | None) -> RerankStrategy | None:
        # Mixed/unfiltered requests keep one common ranking scale. Only explicit
        # table-only requests bypass keyword reranking in the by_type policy.
        if self._vector_only_tables and context_types == ["table_schema"]:
            return None
        return self._rerank
