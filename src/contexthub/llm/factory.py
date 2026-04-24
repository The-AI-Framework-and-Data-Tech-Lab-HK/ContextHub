"""Factory for embedding and chat clients."""

from __future__ import annotations

from contexthub.config import Settings
from contexthub.llm.base import EmbeddingClient, NoOpEmbeddingClient
from contexthub.llm.chat_client import BaseChatClient, NoOpChatClient, OpenAIChatClient
from contexthub.llm.openai_client import OpenAIEmbeddingClient


def create_embedding_client(settings: Settings) -> EmbeddingClient:
    if settings.openai_api_key:
        return OpenAIEmbeddingClient(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.embedding_model,
            expected_dimensions=settings.embedding_dimensions,
        )
    return NoOpEmbeddingClient()


def create_chat_client(settings: Settings) -> BaseChatClient:
    if settings.openai_api_key:
        return OpenAIChatClient(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.chat_model,
        )
    return NoOpChatClient()
