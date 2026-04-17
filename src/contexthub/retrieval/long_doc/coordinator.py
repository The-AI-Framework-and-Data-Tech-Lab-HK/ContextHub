from __future__ import annotations

import logging

from contexthub.db.repository import ScopedRepo

from .result import LongDocRetrievalResult

logger = logging.getLogger(__name__)


class LongDocRetrievalCoordinator:
    def __init__(self):
        self._strategies: dict[str, object] = {}

    def register_strategy(self, name: str, strategy) -> None:
        self._strategies[name] = strategy

    async def retrieve(
        self,
        db: ScopedRepo,
        query: str,
        candidates: list[dict],
        *,
        strategy: str = "tree",
    ) -> list[dict]:
        long_docs = [candidate for candidate in candidates if candidate.get("file_path")]
        if not long_docs or strategy not in self._strategies:
            return candidates

        strat = self._strategies[strategy]
        all_results: list[LongDocRetrievalResult] = []

        if strategy == "tree":
            for doc in long_docs:
                try:
                    base_score = float(doc.get("_rerank_score", doc.get("cosine_similarity", 0.0)))
                    results = await strat.retrieve(
                        db,
                        query,
                        doc["id"],
                        doc["uri"],
                        doc["file_path"],
                        base_score=base_score,
                    )
                except Exception:
                    logger.warning("Tree retrieval failed for %s", doc.get("uri"), exc_info=True)
                    results = []
                all_results.extend(results[:1])
        elif strategy == "keyword":
            try:
                all_results = await strat.retrieve(db, query, long_docs)
            except Exception:
                logger.warning("Keyword retrieval failed", exc_info=True)
                all_results = []

        return self._merge_results(candidates, all_results)

    def _merge_results(
        self,
        candidates: list[dict],
        results: list[LongDocRetrievalResult],
    ) -> list[dict]:
        best_by_context: dict[object, LongDocRetrievalResult] = {}
        for result in results:
            current = best_by_context.get(result.context_id)
            if current is None or result.relevance_score > current.relevance_score:
                best_by_context[result.context_id] = result

        merged: list[dict] = []
        for candidate in candidates:
            result = best_by_context.get(candidate.get("id"))
            if result is None:
                merged.append(candidate)
                continue
            replacement = dict(candidate)
            replacement["snippet"] = result.snippet
            replacement["section_id"] = result.section_id
            replacement["retrieval_strategy"] = result.strategy
            replacement["_rerank_score"] = result.relevance_score
            merged.append(replacement)
        return merged
