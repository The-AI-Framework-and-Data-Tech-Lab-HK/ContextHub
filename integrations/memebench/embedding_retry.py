"""Retry wrapper around an embedding client.

The yunwu proxy occasionally returns ReadTimeout under load; the underlying
OpenAIEmbeddingClient returns None on failure. Batch ingestion needs every node
to get an embedding (else it is invisible to vector search), so we retry with
backoff instead of silently leaving l0_embedding NULL.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class RetryingEmbeddingClient:
    def __init__(self, inner, *, max_attempts: int = 4, base_delay: float = 1.0, max_batch: int | None = None):
        self._inner = inner
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        # Provider-side per-request batch cap (aliyun v4 = 10). None = no cap.
        self._max_batch = max_batch

    async def embed(self, text: str):
        for attempt in range(1, self._max_attempts + 1):
            result = await self._inner.embed(text)
            if result is not None:
                return result
            if attempt < self._max_attempts:
                await asyncio.sleep(self._base_delay * attempt)
        logger.error("Embedding failed after %d attempts", self._max_attempts)
        return None

    async def embed_batch(self, texts: list[str]):
        """Real batched call with whole-batch retry; per-item retry only for stragglers.

        One request embeds all texts (minimizes exposure to the endpoint's
        intermittent stalls). If the batch fails, retry the batch; if individual
        items come back None, retry just those per-item.
        """
        if not texts:
            return []
        # Split into provider-allowed chunks (aliyun caps batch at 10).
        if self._max_batch and len(texts) > self._max_batch:
            out: list = []
            for i in range(0, len(texts), self._max_batch):
                out.extend(await self.embed_batch(texts[i : i + self._max_batch]))
            return out

        results = None
        for attempt in range(1, self._max_attempts + 1):
            results = await self._inner.embed_batch(texts)
            if results and all(r is not None for r in results):
                return results
            if attempt < self._max_attempts:
                await asyncio.sleep(self._base_delay * attempt)
        # Fill any remaining Nones with per-item retry.
        results = results or [None] * len(texts)
        out = []
        for text, r in zip(texts, results):
            out.append(r if r is not None else await self.embed(text))
        return out

    async def close(self):
        close = getattr(self._inner, "close", None)
        if close is not None:
            await close()
