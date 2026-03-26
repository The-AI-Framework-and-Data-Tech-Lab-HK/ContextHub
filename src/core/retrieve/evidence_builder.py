"""Build retrieve evidence payloads."""

from __future__ import annotations

from core.retrieve.semantic_recall import SemanticHit


def build_semantic_evidence(hit: SemanticHit) -> dict:
    return {
        "matched_nodes": [],
        "matched_subgraph": None,
        "matched_uris": list(hit.matched_uris),
    }
