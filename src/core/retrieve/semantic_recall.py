"""Semantic recall against trajectory-level vector index."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import httpx
from openai import OpenAI

from infra.storage.vector.base import VectorStoreAdapter


def _distance_to_score(distance: float | int | None) -> float:
    if distance is None:
        return 0.0
    d = float(distance)
    if d < 0:
        d = 0.0
    return 1.0 / (1.0 + d)


@dataclass
class SemanticHit:
    trajectory_id: str
    semantic_score: float
    matched_uris: list[str]
    raw_hits: list[dict[str, Any]]


@dataclass
class SemanticRecall:
    vector_store: VectorStoreAdapter
    embedding_model: str
    api_key: str
    embedder_base_url: str | None = None
    embedding_mode: str = "multimodal"
    embedding_fn: Callable[[str], list[float]] | None = None

    def _embed_text(self, text: str) -> list[float]:
        client = OpenAI(api_key=self.api_key, base_url=self.embedder_base_url or None)
        resp = client.embeddings.create(model=self.embedding_model, input=text)
        return [float(x) for x in resp.data[0].embedding]

    def _embed_multimodal(self, text: str) -> list[float]:
        if not self.embedder_base_url:
            raise ValueError("AMC_EMBEDDING_BASE_URL is required for multimodal embedding mode")
        endpoint = f"{self.embedder_base_url.rstrip('/')}/embeddings/multimodal"
        payload: dict[str, Any] = {
            "model": self.embedding_model,
            "input": [{"type": "text", "text": text}],
            "encoding_format": "float",
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(endpoint, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        row = data.get("data")
        if isinstance(row, dict):
            emb = row.get("embedding")
            if isinstance(emb, list):
                return [float(x) for x in emb]
        if isinstance(row, list) and row and isinstance(row[0], dict):
            emb = row[0].get("embedding")
            if isinstance(emb, list):
                return [float(x) for x in emb]
        raise RuntimeError("invalid multimodal embedding response shape: missing embedding vector")

    def _embed_query(self, query_text: str) -> list[float]:
        if self.embedding_fn is not None:
            return self.embedding_fn(query_text)
        mode = (self.embedding_mode or "multimodal").strip().lower()
        if mode == "text":
            return self._embed_text(query_text)
        if mode == "multimodal":
            return self._embed_multimodal(query_text)
        raise ValueError(f"unsupported embedding mode: {self.embedding_mode}")

    def recall(self, *, tenant_id: str, agent_id: str, query_text: str, top_k: int) -> list[SemanticHit]:
        query_vec = self._embed_query(query_text)
        # Retrieve a wider candidate pool then aggregate by trajectory_id.
        raw_rows = self.vector_store.query(query_vec, top_k=max(int(top_k) * 6, 20))
        grouped: dict[str, dict[str, Any]] = {}
        for row in raw_rows:
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            if str(meta.get("tenant_id") or "") != tenant_id:
                continue
            # Keep current agent scope by default.
            if str(meta.get("agent_id") or "") != agent_id:
                continue
            tid = str(meta.get("trajectory_id") or "").strip()
            if not tid:
                continue
            score = _distance_to_score(row.get("distance"))
            uri = str(meta.get("uri") or "")
            slot = grouped.setdefault(tid, {"score": 0.0, "uris": set(), "rows": []})
            slot["score"] = max(float(slot["score"]), score)
            if uri:
                slot["uris"].add(uri)
            slot["rows"].append(row)

        hits: list[SemanticHit] = []
        for tid, info in grouped.items():
            uris = sorted(str(x) for x in info["uris"])
            hits.append(
                SemanticHit(
                    trajectory_id=tid,
                    semantic_score=float(info["score"]),
                    matched_uris=uris,
                    raw_hits=info["rows"],
                )
            )
        hits.sort(key=lambda x: x.semantic_score, reverse=True)
        return hits[: max(1, int(top_k))]
