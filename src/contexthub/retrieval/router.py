"""RetrievalRouter: strategy dispatch for retrieval and reranking."""

from __future__ import annotations

from contexthub.retrieval.rerank import KeywordRerankStrategy, RerankStrategy


class RetrievalRouter:
    """Strategy dispatcher: decides which retrieval and rerank strategy to use."""

    def __init__(self, rerank_strategy: RerankStrategy):
        self._rerank = rerank_strategy

    @classmethod
    def default(cls) -> RetrievalRouter:
        return cls(rerank_strategy=KeywordRerankStrategy())

    @property
    def rerank(self) -> RerankStrategy:
        return self._rerank
