"""URI-driven trajectory L0/L1 vector indexing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import httpx
from openai import OpenAI

from infra.storage.vector.base import VectorStoreAdapter


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_parent_uri(*, scope: str, owner_space: str, trajectory_id: str) -> str:
    s = (scope or "agent").strip().lower()
    if s not in {"agent", "team", "datalake", "user"}:
        s = "agent"
    return f"ctx://{s}/{owner_space}/memories/trajectories/{trajectory_id}/"


@dataclass
class TrajectoryVectorIndexer:
    """
    Build/update trajectory-level vectors from URI-targeted source files.

    Embedding source of truth:
    - file content under `uri` target (.abstract.md / .overview.md)
    """

    vector_store: VectorStoreAdapter
    embedding_model: str
    api_key: str
    embedder_base_url: str | None = None
    embedding_mode: str = "multimodal"
    include_levels: tuple[int, ...] = (0, 1)
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

    def _embed(self, text: str) -> list[float]:
        if self.embedding_fn is not None:
            return self.embedding_fn(text)
        mode = (self.embedding_mode or "multimodal").strip().lower()
        if mode == "text":
            return self._embed_text(text)
        if mode == "multimodal":
            return self._embed_multimodal(text)
        raise ValueError(f"unsupported embedding mode: {self.embedding_mode}")

    def index_trajectory(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
        task_type: str | None,
        base_path: str,
        lifecycle_status: str = "active",
        stale_flag: bool = False,
    ) -> dict[str, Any]:
        base = Path(base_path)
        parent_uri = _build_parent_uri(
            scope=scope,
            owner_space=owner_space,
            trajectory_id=trajectory_id,
        )
        files: list[tuple[int, str, Path]] = [
            (0, f"{parent_uri}.abstract.md", base / ".abstract.md"),
            (1, f"{parent_uri}.overview.md", base / ".overview.md"),
        ]
        target = [x for x in files if x[0] in set(self.include_levels)]
        ids = [_md5(f"{tenant_id}:{uri}") for _, uri, _ in target]
        existing = self.vector_store.get_metadatas(ids)

        now_iso = datetime.now(UTC).isoformat()
        upserts: list[dict[str, Any]] = []
        skipped_unchanged = 0
        missing_files: list[str] = []
        for level, uri, file_path in target:
            if not file_path.exists():
                missing_files.append(str(file_path))
                continue
            # Embedding must use source file raw content.
            text = file_path.read_text(encoding="utf-8")
            content_sha = _sha256(text)
            vid = _md5(f"{tenant_id}:{uri}")
            old = existing.get(vid) or {}
            if str(old.get("content_sha256") or "") == content_sha:
                skipped_unchanged += 1
                continue
            emb = self._embed(text)
            upserts.append(
                {
                    "id": vid,
                    "embedding": emb,
                    "metadata": {
                        "uri": uri,
                        "parent_uri": parent_uri,
                        "level": int(level),
                        "account_id": account_id,
                        "scope": scope,
                        "owner_space": owner_space,
                        "tenant_id": tenant_id,
                        "trajectory_id": trajectory_id,
                        "agent_id": agent_id,
                        "task_type": task_type or "",
                        "status": lifecycle_status,
                        "lifecycle_status": lifecycle_status,
                        "stale_flag": bool(stale_flag),
                        "updated_at": now_iso,
                        "content_sha256": content_sha,
                    },
                }
            )
        self.vector_store.upsert_embeddings(upserts)
        return {
            "enabled": True,
            "source": "file_uri_content",
            "collection_op": "upsert",
            "target_levels": list(self.include_levels),
            "candidate_docs": len(target),
            "upserted_docs": len(upserts),
            "skipped_unchanged_docs": skipped_unchanged,
            "missing_files": missing_files,
        }
