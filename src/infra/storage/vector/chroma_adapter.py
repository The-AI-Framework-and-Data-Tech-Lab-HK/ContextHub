"""Chroma vector adapter skeleton for future indexing/retrieve pipeline."""

from __future__ import annotations

from typing import Any

import chromadb

from infra.storage.vector.base import VectorStoreAdapter


class ChromaVectorAdapter(VectorStoreAdapter):
    """
    Minimal adapter shell.

    Note:
    - Actual embedding queue / upsert / query integration is phase 1.1+.
    - This class exists to keep storage backend selection pluggable.
    - When implemented, embedding input must be loaded from `uri` file content
      (`.abstract.md/.overview.md` full text), not copied payload text.
    """

    def __init__(
        self,
        *,
        collection_name: str,
        persist_dir: str,
        distance: str = "cosine",
    ) -> None:
        self.collection_name = collection_name
        client = chromadb.PersistentClient(path=persist_dir)
        self.collection = client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": distance},
        )

    def get_metadatas(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        out = self.collection.get(ids=ids, include=["metadatas"])
        result: dict[str, dict[str, Any]] = {}
        got_ids = out.get("ids") or []
        got_meta = out.get("metadatas") or []
        for i, vid in enumerate(got_ids):
            if not isinstance(vid, str):
                continue
            meta = got_meta[i] if i < len(got_meta) and isinstance(got_meta[i], dict) else {}
            result[vid] = meta
        return result

    def upsert_embeddings(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        ids: list[str] = []
        embeddings: list[list[float]] = []
        metadatas: list[dict[str, Any]] = []
        for record in records:
            vid = str(record.get("id") or "").strip()
            emb = record.get("embedding")
            meta = record.get("metadata") or {}
            if not vid or not isinstance(emb, list):
                continue
            ids.append(vid)
            embeddings.append([float(x) for x in emb])
            metadatas.append(meta if isinstance(meta, dict) else {})
        if not ids:
            return
        self.collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)

    def query(self, embedding: list[float], top_k: int) -> list[dict[str, Any]]:
        out = self.collection.query(
            query_embeddings=[embedding],
            n_results=max(1, int(top_k)),
            include=["metadatas", "distances"],
        )
        ids = (out.get("ids") or [[]])[0]
        metas = (out.get("metadatas") or [[]])[0]
        dists = (out.get("distances") or [[]])[0]
        rows: list[dict[str, Any]] = []
        for i, vid in enumerate(ids):
            rows.append(
                {
                    "id": vid,
                    "metadata": metas[i] if i < len(metas) else {},
                    "distance": dists[i] if i < len(dists) else None,
                }
            )
        return rows
