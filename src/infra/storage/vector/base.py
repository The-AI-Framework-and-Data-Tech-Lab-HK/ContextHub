"""Vector storage protocol for pluggable embedding backends."""

from __future__ import annotations

from typing import Any, Protocol


class VectorStoreAdapter(Protocol):
    """Minimal vector-store adapter protocol.

    IMPORTANT:
    - Embedding inputs MUST come from source file content by URI.
    - Adapters should treat `uri` as the canonical anchor and avoid storing raw document body.
    - Repeated commit should compare `content_sha256`; if changed, re-embed + upsert.
    """

    def get_metadatas(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Get existing metadata by vector id."""

    def upsert_embeddings(self, records: list[dict[str, Any]]) -> None:
        """Insert/update vectors with metadata."""

    def query(self, embedding: list[float], top_k: int) -> list[dict[str, Any]]:
        """Query nearest neighbor documents."""
