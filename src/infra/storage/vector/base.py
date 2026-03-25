"""Vector storage protocol for pluggable embedding backends."""

from __future__ import annotations

from typing import Any, Protocol


class VectorStoreAdapter(Protocol):
    """Minimal vector-store adapter protocol."""

    def upsert_documents(self, docs: list[dict[str, Any]]) -> None:
        """Insert/update embedding documents."""

    def query(self, embedding: list[float], top_k: int) -> list[dict[str, Any]]:
        """Query nearest neighbor documents."""
