"""Factory for pluggable vector storage adapters."""

from __future__ import annotations

from app.config import AppSettings
from infra.storage.vector.base import VectorStoreAdapter
from infra.storage.vector.chroma_adapter import ChromaVectorAdapter


def build_vector_store_adapter(settings: AppSettings) -> VectorStoreAdapter | None:
    """
    Build vector-store adapter by backend name.

    Current support:
    - chroma: returns adapter shell (integration pending)
    - none/disabled: returns None
    """
    backend = (settings.vector_store_backend or "chroma").strip().lower()
    if backend in {"none", "disabled"}:
        return None
    if backend == "chroma":
        return ChromaVectorAdapter(collection_name="amc_trajectory_index")
    print(f"[AMC] unsupported vector_store backend '{backend}', vector adapter disabled")
    return None
