"""OpenAI embedding client using httpx."""

from __future__ import annotations

import logging

import httpx

from contexthub.llm.base import EmbeddingClient  # noqa: TC001

logger = logging.getLogger(__name__)


class OpenAIEmbeddingClient:
    """Real OpenAI embedding client implementing EmbeddingClient protocol."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "text-embedding-3-small",
        expected_dimensions: int | None = None,
    ):
        self._api_key = api_key
        self._model = model
        self._expected_dimensions = expected_dimensions
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )

    async def embed(self, text: str) -> list[float] | None:
        try:
            resp = await self._client.post(
                "/embeddings",
                json={"input": text, "model": self._model},
            )
            resp.raise_for_status()
            data = resp.json().get("data") or []
        except Exception:
            logger.exception("OpenAI embedding failed")
            return None

        if not data:
            logger.error("OpenAI embedding response was empty for model=%s", self._model)
            return None
        return self._validate_embedding(data[0].get("embedding"), operation="single")

    async def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        if not texts:
            return []

        try:
            resp = await self._client.post(
                "/embeddings",
                json={"input": texts, "model": self._model},
            )
            resp.raise_for_status()
            data = resp.json().get("data") or []
        except Exception:
            logger.exception("OpenAI batch embedding failed")
            return [None] * len(texts)

        if len(data) != len(texts):
            logger.error(
                "OpenAI batch embedding returned unexpected item count: expected=%s got=%s model=%s",
                len(texts),
                len(data),
                self._model,
            )
            return [None] * len(texts)

        embeddings: list[list[float] | None] = [None] * len(texts)
        for item in data:
            index = item.get("index")
            if not isinstance(index, int) or not 0 <= index < len(texts):
                logger.error(
                    "OpenAI batch embedding returned invalid index=%r for model=%s",
                    index,
                    self._model,
                )
                return [None] * len(texts)
            embeddings[index] = self._validate_embedding(
                item.get("embedding"),
                operation=f"batch[{index}]",
            )
        return embeddings

    async def close(self) -> None:
        await self._client.aclose()

    def _validate_embedding(
        self,
        embedding: object,
        *,
        operation: str,
    ) -> list[float] | None:
        if not isinstance(embedding, list):
            logger.error(
                "OpenAI embedding response for %s had invalid payload type=%s model=%s",
                operation,
                type(embedding).__name__,
                self._model,
            )
            return None

        if self._expected_dimensions is not None:
            actual_dimensions = len(embedding)
            if actual_dimensions == self._expected_dimensions:
                return embedding
            if actual_dimensions < self._expected_dimensions:
                logger.info(
                    "Padding embedding for %s from %s to %s dimensions for model=%s",
                    operation,
                    actual_dimensions,
                    self._expected_dimensions,
                    self._model,
                )
                return embedding + [0.0] * (self._expected_dimensions - actual_dimensions)

            logger.error(
                "OpenAI embedding dimension mismatch for %s: expected_at_most=%s got=%s model=%s",
                operation,
                self._expected_dimensions,
                actual_dimensions,
                self._model,
            )
            return None

        return embedding
