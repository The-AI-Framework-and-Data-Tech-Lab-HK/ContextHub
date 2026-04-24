from __future__ import annotations

import logging
import re

from contexthub.db.repository import ScopedRepo

from .result import LongDocRetrievalResult

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
    "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "what",
    "when", "where", "why", "with",
}


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
            fallback_docs: list[dict] = []
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
                    if self._should_fallback(query, best[0]):
                        fallback_docs.append(doc)
                else:
                    fallback_docs.append(doc)
            keyword_strategy = self._strategies.get("keyword")
            if fallback_docs and keyword_strategy is not None:
                try:
                    keyword_results = await keyword_strategy.retrieve(db, query, fallback_docs)
                except Exception:
                    logger.warning("Keyword fallback retrieval failed", exc_info=True)
                    keyword_results = []
                all_results = self._replace_results_for_contexts(
                    all_results,
                    keyword_results,
                    context_ids={doc["id"] for doc in fallback_docs},
                )
        elif strategy == "keyword":
            try:
                all_results = await strat.retrieve(db, query, long_docs)
            except Exception:
                logger.warning("Keyword retrieval failed", exc_info=True)
                all_results = []

        return self._merge_results(candidates, all_results)

    def _should_fallback(self, query: str, result: LongDocRetrievalResult) -> bool:
        if not result.snippet:
            return True
        query_tokens = self._query_tokens(query)
        if not query_tokens:
            return False
        snippet_lower = result.snippet.lower()
        snippet_norm = self._normalize_text(result.snippet)
        query_norm = self._normalize_text(query)
        if query_norm and query_norm in snippet_norm:
            return False
        coverage = sum(1 for token in query_tokens if token in snippet_lower) / len(query_tokens)
        return coverage < 0.6

    def _replace_results_for_contexts(
        self,
        existing: list[LongDocRetrievalResult],
        replacements: list[LongDocRetrievalResult],
        *,
        context_ids: set[object],
    ) -> list[LongDocRetrievalResult]:
        by_context = {result.context_id: result for result in existing if result.context_id not in context_ids}
        for result in existing:
            if result.context_id not in by_context and result.context_id not in context_ids:
                by_context[result.context_id] = result
        for result in replacements:
            by_context[result.context_id] = result
        missing = context_ids - set(by_context)
        for result in existing:
            if result.context_id in missing and result.context_id not in by_context:
                by_context[result.context_id] = result
        return list(by_context.values())

    def _query_tokens(self, text: str) -> list[str]:
        tokens: list[str] = []
        seen: set[str] = set()
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            if len(token) < 3 or token in _STOPWORDS or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
        return tokens

    def _normalize_text(self, text: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", text.lower()))

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
