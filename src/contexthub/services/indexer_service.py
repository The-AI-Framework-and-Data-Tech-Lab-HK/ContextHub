"""IndexerService: content summarisation + embedding hook."""

from __future__ import annotations

import logging
from uuid import UUID

from contexthub.db.repository import ScopedRepo
from contexthub.generation.base import ContentGenerator, GeneratedContent
from contexthub.llm.base import EmbeddingClient

logger = logging.getLogger(__name__)


class IndexerService:
    def __init__(self, content_generator: ContentGenerator, embedding_client: EmbeddingClient):
        self._generator = content_generator
        self._embedding = embedding_client

    async def generate(
        self,
        context_type: str,
        raw_content: str,
        metadata: dict | None = None,
    ) -> GeneratedContent:
        return self._generator.generate(context_type, raw_content, metadata)

    async def embed_l0(self, text: str) -> list[float] | None:
        return await self._embedding.embed(text)

    async def update_embedding(
        self, db: ScopedRepo, context_id: UUID, l0_text: str
    ) -> bool:
        """Generate and write l0_embedding for a single context. Returns success."""
        embedding = await self._embedding.embed(l0_text)
        if embedding is None:
            return False
        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
        await db.execute(
            "UPDATE contexts SET l0_embedding = $1::vector WHERE id = $2",
            embedding_str, context_id,
        )
        return True

    async def clear_embedding(self, db: ScopedRepo, context_id: UUID) -> None:
        """Clear l0_embedding for a context (e.g. on archive)."""
        await db.execute(
            "UPDATE contexts SET l0_embedding = NULL WHERE id = $1",
            context_id,
        )

    async def backfill_embeddings(
        self, db: ScopedRepo, batch_size: int = 100
    ) -> int:
        """Backfill missing l0_embedding for active/stale contexts. Returns count filled."""
        rows = await db.fetch(
            """
            SELECT id, l0_content FROM contexts
            WHERE l0_embedding IS NULL
              AND l0_content IS NOT NULL
              AND status IN ('active', 'stale')
            LIMIT $1
            """,
            batch_size,
        )
        if not rows:
            return 0

        # Try batch if client supports it
        if hasattr(self._embedding, "embed_batch"):
            texts = [r["l0_content"] for r in rows]
            embeddings = await self._embedding.embed_batch(texts)
            count = 0
            for row, emb in zip(rows, embeddings):
                if emb is not None:
                    embedding_str = "[" + ",".join(str(x) for x in emb) + "]"
                    await db.execute(
                        "UPDATE contexts SET l0_embedding = $1::vector WHERE id = $2",
                        embedding_str, row["id"],
                    )
                    count += 1
            return count

        # Fallback: one by one
        count = 0
        for row in rows:
            success = await self.update_embedding(db, row["id"], row["l0_content"])
            if success:
                count += 1
        return count
