"""IndexerService: content summarisation + embedding hook."""

from __future__ import annotations

import logging
from uuid import UUID

from contexthub.db.repository import ScopedRepo
from contexthub.generation.base import ContentGenerator, GeneratedContent
from contexthub.llm.base import EmbeddingClient

logger = logging.getLogger(__name__)


class IndexerService:
    def __init__(
        self,
        content_generator: ContentGenerator,
        embedding_client: EmbeddingClient,
        embedding_dimensions: int | None = None,
    ):
        self._generator = content_generator
        self._embedding = embedding_client
        self._embedding_dimensions = embedding_dimensions

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
        try:
            embedding = await self._embedding.embed(l0_text)
        except Exception:
            logger.exception("Failed to generate embedding for context_id=%s", context_id)
            return False
        return await self._write_embedding(db, context_id, embedding)

    async def clear_embedding(self, db: ScopedRepo, context_id: UUID) -> None:
        """Clear l0_embedding for a context (e.g. on archive)."""
        try:
            await db.execute(
                "UPDATE contexts SET l0_embedding = NULL WHERE id = $1",
                context_id,
            )
        except Exception:
            logger.exception("Failed to clear embedding for context_id=%s", context_id)

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
            try:
                embeddings = await self._embedding.embed_batch(texts)
            except Exception:
                logger.exception("Failed to batch-generate embeddings during backfill")
                embeddings = [None] * len(rows)

            if len(embeddings) != len(rows):
                logger.error(
                    "Embedding batch result count mismatch during backfill: expected=%s got=%s",
                    len(rows),
                    len(embeddings),
                )
                embeddings = [None] * len(rows)

            count = 0
            for row, emb in zip(rows, embeddings):
                success = await self._write_embedding(db, row["id"], emb)
                if success:
                    count += 1
            return count

        # Fallback: one by one
        count = 0
        for row in rows:
            success = await self.update_embedding(db, row["id"], row["l0_content"])
            if success:
                count += 1
        return count

    async def _write_embedding(
        self,
        db: ScopedRepo,
        context_id: UUID,
        embedding: list[float] | None,
    ) -> bool:
        embedding_str = self._serialize_embedding(embedding, context_id=context_id)
        if embedding_str is None:
            return False

        try:
            await db.execute(
                "UPDATE contexts SET l0_embedding = $1::vector WHERE id = $2",
                embedding_str,
                context_id,
            )
        except Exception:
            logger.exception("Failed to persist embedding for context_id=%s", context_id)
            return False
        return True

    def _serialize_embedding(
        self,
        embedding: list[float] | None,
        *,
        context_id: UUID,
    ) -> str | None:
        if embedding is None:
            return None

        if (
            self._embedding_dimensions is not None
            and len(embedding) != self._embedding_dimensions
        ):
            logger.error(
                "Embedding dimension mismatch for context_id=%s: expected=%s got=%s",
                context_id,
                self._embedding_dimensions,
                len(embedding),
            )
            return None

        return "[" + ",".join(str(x) for x in embedding) + "]"
