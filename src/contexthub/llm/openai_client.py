"""OpenAI embedding client using httpx."""

from __future__ import annotations

import logging

import httpx

from contexthub.llm.base import EmbeddingClient  # noqa: TC001

logger = logging.getLogger(__name__)


class OpenAIEmbeddingClient:
    """Real OpenAI embedding client implementing EmbeddingClient protocol."""

    def __init__(self, api_key: str, model: str = "text-embedding-3-small"):
        self._api_key = api_key
        self._model = model
        self._client = httpx.AsyncClient(
            base_url="https://api.openai.com/v1",
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
            return resp.json()["data"][0]["embedding"]
        except Exception:
            logger.exception("OpenAI embedding failed")
            return None

    async def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        try:
            resp = await self._client.post(
                "/embeddings",
                json={"input": texts, "model": self._model},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
            # OpenAI returns embeddings sorted by index
            sorted_data = sorted(data, key=lambda x: x["index"])
            return [d["embedding"] for d in sorted_data]
        except Exception:
            logger.exception("OpenAI batch embedding failed")
            return [None] * len(texts)

    async def close(self) -> None:
        await self._client.aclose()
