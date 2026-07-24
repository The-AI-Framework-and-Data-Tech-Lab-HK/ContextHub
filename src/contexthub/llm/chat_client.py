"""Chat completion clients (no OpenAI SDK)."""

from __future__ import annotations

import asyncio
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
        # Last response's API usage block (prompt/completion/total tokens), or
        # None if the endpoint returned none. Read by cost meters that want real
        # token counts instead of a char/4 estimate. Overwritten each complete().
        self.last_usage: dict[str, int] | None = None
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        self.last_usage = None
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        # Proxy gateways (e.g. yunwu) reply 429 with a short cooldown when a key
        # is rate-limited or briefly flagged. Back off and retry only on 429;
        # every other error is raised immediately, unchanged.
        backoffs = (30.0, 60.0, 120.0)
        for attempt in range(len(backoffs) + 1):
            try:
                resp = await self._client.post("/chat/completions", json=payload)
                resp.raise_for_status()
                data = resp.json()
                break
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429 and attempt < len(backoffs):
                    wait = backoffs[attempt]
                    logger.warning(
                        "chat completion 429, backing off %.0fs (attempt %d)",
                        wait,
                        attempt + 1,
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.exception("OpenAI chat completion failed")
                raise
            except Exception:
                logger.exception("OpenAI chat completion failed")
                raise

        usage = data.get("usage")
        if isinstance(usage, dict):
            self.last_usage = {
                "prompt_tokens": usage.get("prompt_tokens") or 0,
                "completion_tokens": usage.get("completion_tokens") or 0,
                "total_tokens": usage.get("total_tokens") or 0,
            }

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
