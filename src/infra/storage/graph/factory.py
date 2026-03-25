"""Factory for pluggable graph storage writers."""

from __future__ import annotations

from app.config import AppSettings
from infra.storage.graph.base import GraphStoreWriter
from infra.storage.graph.neo4j_adapter import Neo4jGraphWriter


def build_graph_store_writer(settings: AppSettings) -> GraphStoreWriter | None:
    """
    Build graph writer adapter by configured backend.

    Current support:
    - neo4j: writes raw/clean graphs to Neo4j
    - localfs/none: disable graph backend writing
    """
    backend = (settings.graph_store_backend or "neo4j").strip().lower()
    if backend in {"none", "disabled", "localfs"}:
        return None
    if backend != "neo4j":
        print(f"[AMC] unsupported graph_store backend '{backend}', graph backend disabled")
        return None
    if not (settings.neo4j_uri and settings.neo4j_user and settings.neo4j_password):
        print("[AMC] neo4j backend configured but AMC_NEO4J_URI/USER/PASSWORD incomplete")
        return None
    try:
        writer = Neo4jGraphWriter(
            uri=settings.neo4j_uri,
            user=settings.neo4j_user,
            password=settings.neo4j_password,
            database=settings.neo4j_database,
        )
        writer.verify_connectivity()
        return writer
    except Exception as exc:
        print(f"[AMC] neo4j graph writer disabled: {type(exc).__name__}: {exc}")
        return None
