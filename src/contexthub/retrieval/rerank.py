"""Rerank strategies: ABC + BM25 keyword implementation."""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from collections import Counter


class RerankStrategy(ABC):
    @abstractmethod
    async def rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        """Rerank candidates by relevance. Each candidate must have l1_content."""
        ...


class KeywordRerankStrategy(RerankStrategy):
    """BM25 keyword scoring. Zero LLM calls, low latency."""

    async def rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        query_tokens = self._tokenize(query)
        if not query_tokens or not candidates:
            return candidates

        doc_count = len(candidates)
        doc_freq: Counter = Counter()
        doc_token_lists = []
        for c in candidates:
            doc_tokens = self._tokenize(c.get("l1_content") or c.get("l0_content") or "")
            doc_token_lists.append(doc_tokens)
            for token in set(doc_tokens):
                doc_freq[token] += 1

        avg_dl = sum(len(d) for d in doc_token_lists) / max(doc_count, 1)
        k1, b = 1.5, 0.75

        scored = []
        for c, doc_tokens in zip(candidates, doc_token_lists):
            score = self._bm25_score(query_tokens, doc_tokens, doc_freq, doc_count, avg_dl, k1, b)
            scored.append({**c, "_rerank_score": score})

        return sorted(scored, key=lambda x: x["_rerank_score"], reverse=True)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return re.findall(r"\w+", text.lower())

    @staticmethod
    def _bm25_score(
        query_tokens: list[str],
        doc_tokens: list[str],
        doc_freq: Counter,
        n_docs: int,
        avg_dl: float,
        k1: float,
        b: float,
    ) -> float:
        tf = Counter(doc_tokens)
        dl = len(doc_tokens)
        score = 0.0
        for qt in query_tokens:
            df = doc_freq.get(qt, 0)
            if df == 0:
                continue
            idf = math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)
            term_tf = tf.get(qt, 0)
            numerator = term_tf * (k1 + 1)
            denominator = term_tf + k1 * (1 - b + b * dl / max(avg_dl, 1e-9))
            score += idf * numerator / denominator
        return score
