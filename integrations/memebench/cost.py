"""Cost metering for LLM calls.

Wraps a BaseChatClient to count calls and estimate tokens. Two separate
instances (answer vs oracle) let the report split answer cost from the
propagation-oracle cost that maps to MEME's ~70x cost story.
"""

from __future__ import annotations

from contexthub.llm.chat_client import BaseChatClient


class CountingChatClient(BaseChatClient):
    """Delegates to a real chat client while counting calls and tokens.

    Prefers the inner client's real API usage block (``last_usage``, exposed by
    OpenAIChatClient); falls back to a len(text)//4 char-per-token estimate only
    when the endpoint returns no usage. ``tokens_are_real`` reports whether every
    call so far supplied real usage. Call count is always exact.
    """

    def __init__(self, inner: BaseChatClient):
        self._inner = inner
        self.call_count = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._estimated_calls = 0  # calls that fell back to char/4

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        self.call_count += 1
        result = await self._inner.complete(prompt, max_tokens=max_tokens)
        usage = getattr(self._inner, "last_usage", None)
        if isinstance(usage, dict) and usage.get("total_tokens"):
            self.prompt_tokens += usage.get("prompt_tokens", 0)
            self.completion_tokens += usage.get("completion_tokens", 0)
        else:
            self._estimated_calls += 1
            self.prompt_tokens += len(prompt) // 4
            self.completion_tokens += len(result or "") // 4
        return result

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def tokens_are_real(self) -> bool:
        """True iff every counted call supplied a real API usage block."""
        return self.call_count > 0 and self._estimated_calls == 0

    def snapshot(self) -> dict[str, int | bool]:
        return {
            "calls": self.call_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "tokens_are_real": self.tokens_are_real,
            "estimated_calls": self._estimated_calls,
        }

    def reset(self) -> None:
        self.call_count = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._estimated_calls = 0

    async def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is not None:
            await close()
