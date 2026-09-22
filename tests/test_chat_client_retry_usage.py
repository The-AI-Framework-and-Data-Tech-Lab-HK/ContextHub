from __future__ import annotations

import httpx
import pytest

from contexthub.llm import chat_client as chat_module
from contexthub.llm.chat_client import OpenAIChatClient
from integrations.memebench.cost import CountingChatClient


class _ScriptedAsyncClient:
    script: list[object] = []

    def __init__(self, *args, **kwargs):
        pass

    async def post(self, *args, **kwargs):
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self):
        return None


def _response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("POST", "https://fixture.invalid/chat/completions"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first",
    (
        _response(429, {"error": "rate limited"}),
        _response(503, {"error": "gateway unavailable"}),
        httpx.ReadTimeout("timed out"),
    ),
)
async def test_retry_without_attempt_usage_is_preserved_and_incomplete(
    monkeypatch, first: object
):
    async def no_sleep(_seconds):
        return None

    _ScriptedAsyncClient.script = [
        first,
        _response(
            200,
            {
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 2,
                    "total_tokens": 9,
                },
                "choices": [{"message": {"content": "ok"}}],
            },
        ),
    ]
    monkeypatch.setattr(chat_module.httpx, "AsyncClient", _ScriptedAsyncClient)
    monkeypatch.setattr(chat_module.asyncio, "sleep", no_sleep)
    inner = OpenAIChatClient("secret", base_url="https://fixture.invalid")
    counted = CountingChatClient(inner, model="fixture")

    assert await counted.complete("prompt") == "ok"
    snapshot = counted.snapshot()
    assert snapshot["retry_attempts"] == 1
    assert snapshot["retry_usage_unknown"] is True
    assert snapshot["prompt_tokens"] == 7
    assert snapshot["completion_tokens"] == 2
    assert inner.last_attempt_count == 2
    assert [row["usage"] for row in inner.last_attempts] == [
        None,
        {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
    ]


@pytest.mark.asyncio
async def test_retry_sums_every_attempt_when_all_usage_is_real(monkeypatch):
    async def no_sleep(_seconds):
        return None

    _ScriptedAsyncClient.script = [
        _response(
            503,
            {
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 0,
                    "total_tokens": 3,
                }
            },
        ),
        _response(
            200,
            {
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 2,
                    "total_tokens": 9,
                },
                "choices": [{"message": {"content": "ok"}}],
            },
        ),
    ]
    monkeypatch.setattr(chat_module.httpx, "AsyncClient", _ScriptedAsyncClient)
    monkeypatch.setattr(chat_module.asyncio, "sleep", no_sleep)
    counted = CountingChatClient(
        OpenAIChatClient("secret", base_url="https://fixture.invalid"),
        model="fixture",
    )

    await counted.complete("prompt")
    assert counted.snapshot() == {
        "model": "fixture",
        "calls": 1,
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
        "tokens_are_real": True,
        "estimated_calls": 0,
        "retry_attempts": 1,
        "retry_usage_unknown": False,
    }
