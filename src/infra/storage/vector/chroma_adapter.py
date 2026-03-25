"""Chroma vector adapter skeleton for future indexing/retrieve pipeline."""

from __future__ import annotations

from typing import Any

from infra.storage.vector.base import VectorStoreAdapter


class ChromaVectorAdapter(VectorStoreAdapter):
    """
    Minimal adapter shell.

    Note:
    - Actual embedding queue / upsert / query integration is phase 1.1+.
    - This class exists to keep storage backend selection pluggable.
    """

    def __init__(self, *, collection_name: str) -> None:
        self.collection_name = collection_name

    def upsert_documents(self, docs: list[dict[str, Any]]) -> None:
        raise NotImplementedError("Chroma upsert integration is not implemented yet")

    def query(self, embedding: list[float], top_k: int) -> list[dict[str, Any]]:
        raise NotImplementedError("Chroma query integration is not implemented yet")
