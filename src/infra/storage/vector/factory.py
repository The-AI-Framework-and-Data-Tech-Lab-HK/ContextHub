"""Factory for pluggable vector storage adapters."""

from __future__ import annotations

from app.config import AppSettings
from infra.storage.vector.base import VectorStoreAdapter
from infra.storage.vector.chroma_adapter import ChromaVectorAdapter
from infra.storage.vector.pgvector_adapter import PgVectorAdapter


def build_vector_store_adapter(settings: AppSettings) -> VectorStoreAdapter | None:
    """
    Build vector-store adapter by backend name.

    Current support:
    - pgvector: PostgreSQL + pgvector
    - chroma: persistent local adapter
    - none/disabled: returns None
    """
    backend = (settings.vector_store_backend or "pgvector").strip().lower()
    if backend in {"none", "disabled"}:
        return None
    if backend == "pgvector":
        dsn = (settings.pgvector_dsn or "").strip()
        if not dsn:
            print("[AMC] vector_store backend=pgvector but AMC_PGVECTOR_DSN is empty, adapter disabled")
            return None
        return PgVectorAdapter(
            dsn=dsn,
            schema=settings.pgvector_schema,
            table=settings.pgvector_table,
        )
    if backend == "chroma":
        return ChromaVectorAdapter(
            collection_name=settings.vector_collection_name,
            persist_dir=settings.chroma_persist_dir,
            distance=settings.vector_distance,
        )
    print(f"[AMC] unsupported vector_store backend '{backend}', vector adapter disabled")
    return None
