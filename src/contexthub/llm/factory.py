"""Factory for embedding clients."""

from __future__ import annotations

from contexthub.config import Settings
from contexthub.llm.base import EmbeddingClient, NoOpEmbeddingClient
from contexthub.llm.openai_client import OpenAIEmbeddingClient


def create_embedding_client(settings: Settings) -> EmbeddingClient:
    if settings.openai_api_key:
        return OpenAIEmbeddingClient(
            api_key=settings.openai_api_key,
            model=settings.embedding_model,
            expected_dimensions=settings.embedding_dimensions,
        )
    return NoOpEmbeddingClient()
