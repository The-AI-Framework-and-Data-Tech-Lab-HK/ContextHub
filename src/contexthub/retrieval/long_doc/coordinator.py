from __future__ import annotations

import logging
from dataclasses import replace
from typing import Literal

from contexthub.db.repository import ScopedRepo

from .result import LongDocRetrievalResult

logger = logging.getLogger(__name__)


def _min_max(scores: dict) -> dict:
    """Normalize available scores; a constant/empty route adds no distinction."""
    if not scores:
        return {}
    low, high = min(scores.values()), max(scores.values())
    return {key: (value - low) / (high - low) if high > low else 0.0
            for key, value in scores.items()}


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
                best = results[:1]
                if best:
                    all_results.extend(best)
        elif strategy == "keyword":
            try:
                all_results = await strat.retrieve(db, query, long_docs)
            except Exception:
                logger.warning("Keyword retrieval failed", exc_info=True)
                all_results = []

        return self._merge_results(candidates, all_results)

    async def search_originals(
        self, db, query, documents, *, query_embedding=None, top_k=30,
        original_fusion: Literal["rrf", "weighted"] = "weighted",
    ):
        """Fuse original ranges using RRF or fixed normalized vector/BM25 weights."""
        if not documents:
            return []
        keyword = self._strategies.get("keyword")
        lexical = await keyword.retrieve(db, query, documents) if keyword is not None else []
        vector = self._strategies.get("vector")
        semantic = []
        if vector is not None and query_embedding is not None:
            semantic = await vector.retrieve(db, query_embedding, documents, top_k)
        versions = {doc["id"]: doc["version"] for doc in documents}

        def key(result):
            return result.context_id, versions[result.context_id], result.snippet_offset

        # Keep discovery unchanged. Completing scores must not discover new ranges.
        fused = {key(result): result for result in [*lexical, *semantic]}
        lexical_scores = {key(result): result.relevance_score for result in lexical}
        if original_fusion == "rrf":
            scores = {k: 0.0 for k in fused}
            vector_scores = {key(result): result.relevance_score for result in semantic}
            for route in (lexical, semantic):
                for rank, result in enumerate(sorted(route, key=lambda r: r.relevance_score, reverse=True), 1):
                    scores[key(result)] += 1.0 / (60 + rank)
        elif original_fusion == "weighted":
            vector_scores = {key(result): result.relevance_score for result in semantic}
            if vector is not None and query_embedding is not None:
                missing = [result for result in lexical if key(result) not in vector_scores]
                if missing:
                    completed = await vector.score_candidates(db, query_embedding, documents, missing)
                    vector_scores.update({key(result): result.relevance_score for result in completed})

            # The lexical retriever returns every positive BM25 range, using the full
            # corpus statistics. A missing lexical hit therefore has a genuine zero.
            bm25 = _min_max({k: lexical_scores.get(k, 0.0) for k in fused})
            # Missing valid vectors stay absent from cosine statistics and add no vector score.
            cosine = _min_max(vector_scores)
            scores = {k: 0.8 * cosine.get(k, 0.0) + 0.2 * bm25[k] for k in fused}
        else:
            raise ValueError(f"Unsupported original_fusion: {original_fusion}")
        results = []
        for k, result in fused.items():
            strategy = "hybrid" if k in vector_scores and k in lexical_scores else result.strategy
            results.append(replace(result, strategy=strategy, relevance_score=scores[k]))
        by_id = {doc["id"]: doc for doc in documents}
        return [self._materialize(by_id[result.context_id], result)
                for result in sorted(results, key=lambda r: r.relevance_score, reverse=True)]

    @staticmethod
    def _materialize(candidate: dict, result: LongDocRetrievalResult) -> dict:
        return {**candidate, "snippet": result.snippet, "section_id": result.section_id,
                "snippet_offset": result.snippet_offset, "retrieval_strategy": result.strategy,
                "_rerank_score": result.relevance_score}

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
            merged.append(self._materialize(candidate, result))
        return merged
