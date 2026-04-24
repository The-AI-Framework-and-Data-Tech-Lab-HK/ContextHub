"""Chat completion clients (no OpenAI SDK)."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

logger = logging.getLogger(__name__)


class BaseChatClient(ABC):
    @abstractmethod
    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        """通用文本生成接口。"""


class OpenAIChatClient(BaseChatClient):
    """OpenAI Chat Completions via httpx (aligned with OpenAIEmbeddingClient style)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        timeout: float = 120.0,
    ):
        self._api_key = api_key
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        try:
            resp = await self._client.post(
                "/chat/completions",
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.exception("OpenAI chat completion failed")
            raise

        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if content is None:
            return ""
        return content if isinstance(content, str) else str(content)

    async def close(self) -> None:
        await self._client.aclose()


class NoOpChatClient(BaseChatClient):
    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        return ""
