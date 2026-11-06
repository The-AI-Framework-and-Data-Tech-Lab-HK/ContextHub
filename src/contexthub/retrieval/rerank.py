"""Rerank strategies and shared BM25 scoring over an explicit token corpus."""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from collections import Counter


def bm25_scores(query_tokens: list[str], documents: list[list[str]]) -> list[float]:
    """Score every document using statistics from the entire supplied corpus.

    Include nonmatching documents in ``documents``; filter zero scores only
    afterwards. Parameters and formula are shared with the existing reranker.
    """
    doc_count = len(documents)
    doc_freq = Counter(token for tokens in documents for token in set(tokens))
    avg_dl = sum(len(tokens) for tokens in documents) / max(doc_count, 1)
    k1, b = 1.5, 0.75
    scores = []
    for tokens in documents:
        tf = Counter(tokens)
        dl = len(tokens)
        score = 0.0
        for qt in query_tokens:
            df = doc_freq.get(qt, 0)
            if df == 0:
                continue
            idf = math.log((doc_count - df + 0.5) / (df + 0.5) + 1.0)
            term_tf = tf.get(qt, 0)
            numerator = term_tf * (k1 + 1)
            denominator = term_tf + k1 * (1 - b + b * dl / max(avg_dl, 1e-9))
            score += idf * numerator / denominator
        scores.append(score)
    return scores


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
        documents = [self._tokenize(c.get("l1_content") or c.get("l0_content") or c.get("snippet") or "")
                     for c in candidates]
        scores = bm25_scores(query_tokens, documents)
        scored = [{**c, "_rerank_score": score} for c, score in zip(candidates, scores)]
        return sorted(scored, key=lambda x: x["_rerank_score"], reverse=True)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return re.findall(r"\w+", text.lower())
