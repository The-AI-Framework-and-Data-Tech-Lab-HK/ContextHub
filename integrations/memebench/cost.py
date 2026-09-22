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

    def __init__(self, inner: BaseChatClient, model: str | None = None):
        self._inner = inner
        # Model name this client actually calls, carried into snapshot() so cost
        # accounting can price each bucket at its OWN rate (runs mix gpt-4o-mini /
        # gpt-4.1-mini / gpt-5.5, whose rates differ by ~40x).
        self.model = model or getattr(inner, "_model", None)
        self.call_count = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._estimated_calls = 0  # calls that fell back to char/4
        self.retry_attempts = 0
        self.retry_usage_unknown = False

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        self.call_count += 1
        result: str | None = None
        succeeded = False
        try:
            result = await self._inner.complete(prompt, max_tokens=max_tokens)
            succeeded = True
            return result
        finally:
            attempts = int(getattr(self._inner, "last_attempt_count", 1) or 1)
            self.retry_attempts += max(0, attempts - 1)
            unknown = bool(
                getattr(self._inner, "last_retry_unknown_usage", False)
            )
            self.retry_usage_unknown = self.retry_usage_unknown or unknown
            usage = getattr(self._inner, "last_usage", None)
            if isinstance(usage, dict):
                self.prompt_tokens += int(usage.get("prompt_tokens", 0))
                self.completion_tokens += int(usage.get("completion_tokens", 0))
            elif succeeded and not unknown:
                self._estimated_calls += 1
                self.prompt_tokens += len(prompt) // 4
                self.completion_tokens += len(result or "") // 4

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def tokens_are_real(self) -> bool:
        """True iff every counted call supplied a real API usage block."""
        return self.call_count > 0 and self._estimated_calls == 0

    def snapshot(self) -> dict[str, int | bool | str | None]:
        return {
            "model": self.model,
            "calls": self.call_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "tokens_are_real": self.tokens_are_real,
            "estimated_calls": self._estimated_calls,
            "retry_attempts": self.retry_attempts,
            "retry_usage_unknown": self.retry_usage_unknown,
        }

    def reset(self) -> None:
        self.call_count = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._estimated_calls = 0
        self.retry_attempts = 0
        self.retry_usage_unknown = False

    async def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is not None:
            await close()
