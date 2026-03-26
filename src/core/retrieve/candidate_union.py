"""Candidate union helper.

Phase 2 (semantic-only MVP): candidates come from semantic branch only.
This module keeps the interface ready for later graph-branch union.
"""

from __future__ import annotations

from core.retrieve.semantic_recall import SemanticHit


def union_candidates(semantic_hits: list[SemanticHit]) -> list[SemanticHit]:
    return semantic_hits
