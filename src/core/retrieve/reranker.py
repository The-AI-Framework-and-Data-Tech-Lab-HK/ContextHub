"""Reranker helper.

Phase 2 (semantic-only MVP): final score equals semantic score.
"""

from __future__ import annotations

from core.retrieve.semantic_recall import SemanticHit


def rerank_semantic_only(hits: list[SemanticHit]) -> list[SemanticHit]:
    return sorted(hits, key=lambda x: x.semantic_score, reverse=True)
