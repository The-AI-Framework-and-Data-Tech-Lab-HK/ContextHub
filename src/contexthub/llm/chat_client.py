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
        temperature: float | None = None,
    ):
        self._api_key = api_key
        self._model = model
        # Only sent when explicitly set, so every existing caller's payload is
        # byte-for-byte unchanged. Set by callers reproducing a published
        # protocol that pins it (e.g. MEME's judge: GPT-4o temperature 0).
        self._temperature = temperature
        # Kept so a hung-connection retry can rebuild the client (see complete()).
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # Last response's API usage block (prompt/completion/total tokens), or
        # None if the endpoint returned none. Read by cost meters that want real
        # token counts instead of a char/4 estimate. Overwritten each complete().
        self.last_usage: dict[str, int] | None = None
        self.last_attempt_count = 0
        self.last_retry_unknown_usage = False
        self.last_attempts: list[dict[str, object]] = []
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        self.last_usage = None
        self.last_attempt_count = 0
        self.last_retry_unknown_usage = False
        self.last_attempts = []
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        # Proxy gateways (e.g. yunwu) reply 429 with a short cooldown when a key
        # is rate-limited or briefly flagged; long-run batch evals also hit
        # transient network blips (timeout / connection reset) and gateway 5xx.
        # All of these are retryable — back off and retry so one blip doesn't
        # fail-soft a whole case. A non-429 4xx (bad request) is NOT retryable
        # and is raised immediately, unchanged.
        rl_backoffs = (30.0, 60.0, 120.0)   # 429 rate-limit cooldowns
        net_backoffs = (2.0, 5.0, 10.0)     # transient network / 5xx blips
        # Per-attempt READ deadline, escalating. The openlux proxy intermittently
        # accepts a request and then never answers: measured 2026-08-10, a 230-token
        # prompt (normally ~3s) hung past 120s while the run's average was 8.2s/call.
        # A single flat 120s deadline with no retry killed 4 consecutive single-case
        # runs and cost the model sweeps a 10-17% error rate. 60s is above the
        # slowest legitimate call ever measured here (33.9s) yet finds a hang a
        # minute sooner; escalating hedges the case where slowness is real.
        # Sum (60+90+120 = 270s plus backoff sleeps) must stay well inside the
        # caller's per-case budget, or one hang eats the whole case.
        read_timeouts = (60.0, 90.0, 120.0)
        for attempt in range(len(rl_backoffs) + 1):
            self.last_attempt_count = attempt + 1
            try:
                per_try = read_timeouts[min(attempt, len(read_timeouts) - 1)]
                resp = await self._client.post(
                    "/chat/completions", json=payload,
                    timeout=httpx.Timeout(per_try, connect=10.0),
                )
                resp.raise_for_status()
                data = resp.json()
                self._record_attempt_usage(data, status_code=resp.status_code)
                break
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                try:
                    error_data = exc.response.json()
                except Exception:
                    error_data = {}
                self._record_attempt_usage(error_data, status_code=code)
                if code == 429 and attempt < len(rl_backoffs):
                    wait = rl_backoffs[attempt]
                    logger.warning(
                        "chat completion 429, backing off %.0fs (attempt %d)",
                        wait, attempt + 1,
                    )
                    await asyncio.sleep(wait)
                    continue
                if code >= 500 and attempt < len(net_backoffs):
                    wait = net_backoffs[attempt]
                    logger.warning(
                        "chat completion %d, retrying in %.0fs (attempt %d)",
                        code, wait, attempt + 1,
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.exception("OpenAI chat completion failed")
                raise
            except httpx.ReadTimeout:
                self._record_attempt_usage(None)
                # Was: raise immediately, on the reasoning that a read timeout means
                # slow GENERATION and retrying just stacks more waiting. Measurement
                # on 2026-08-10 refuted that for this proxy — the timing-out calls
                # carried ~230-token prompts that normally return in ~3s, so the
                # connection was hung, not the model slow. Retry, escalating the
                # deadline, and drop the pooled connection first so the retry cannot
                # land on the same hung socket. (RetryingEmbeddingClient already
                # treats this class of fault as retryable; chat was the outlier.)
                if attempt < len(net_backoffs):
                    wait = net_backoffs[attempt]
                    logger.warning(
                        "chat completion read timeout after %.0fs, retrying in %.0fs "
                        "(attempt %d)", per_try, wait, attempt + 1,
                    )
                    try:
                        await self._client.aclose()
                    except Exception:
                        pass
                    self._client = httpx.AsyncClient(
                        base_url=self._base_url,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        timeout=self._timeout,
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.exception("OpenAI chat completion read-timed-out")
                raise
            except (httpx.TransportError, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
                # connection reset/refused, DNS, write/pool timeout — transient blips.
                self._record_attempt_usage(None)
                if attempt < len(net_backoffs):
                    wait = net_backoffs[attempt]
                    logger.warning(
                        "chat completion network error (%s), retrying in %.0fs (attempt %d)",
                        type(exc).__name__, wait, attempt + 1,
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.exception("OpenAI chat completion failed")
                raise
            except Exception:
                logger.exception("OpenAI chat completion failed")
                raise

        known_usages = [
            row["usage"] for row in self.last_attempts if isinstance(row.get("usage"), dict)
        ]
        if known_usages:
            self.last_usage = {
                field: sum(int(usage.get(field, 0)) for usage in known_usages)
                for field in ("prompt_tokens", "completion_tokens", "total_tokens")
            }

        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if content is None:
            return ""
        return content if isinstance(content, str) else str(content)

    def _record_attempt_usage(
        self,
        data: object,
        *,
        status_code: int | None = None,
    ) -> None:
        usage = data.get("usage") if isinstance(data, dict) else None
        normalized: dict[str, int] | None = None
        if isinstance(usage, dict) and all(
            isinstance(usage.get(field), int)
            for field in ("prompt_tokens", "completion_tokens")
        ):
            prompt_tokens = int(usage["prompt_tokens"])
            completion_tokens = int(usage["completion_tokens"])
            normalized = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": int(
                    usage.get("total_tokens", prompt_tokens + completion_tokens)
                ),
            }
        self.last_attempts.append(
            {"status_code": status_code, "usage": normalized}
        )
        if normalized is None:
            self.last_retry_unknown_usage = True

    async def close(self) -> None:
        await self._client.aclose()


class NoOpChatClient(BaseChatClient):
    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        return ""
