import json

import httpx
import pytest
from knowledge_helpers import MemorySink, identity, policy

from contexthub.config import Settings
from contexthub.llm import factory
from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.retry_policy import RecordedCall, ResponseSchemaError
from contexthub.models.knowledge import canonical_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("thinking", [None, False, True])
@pytest.mark.parametrize("recorded", [False, True])
async def test_thinking_setting_reaches_http_and_recorded_request_hash(
    monkeypatch, thinking, recorded
):
    requests = []

    async def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    monkeypatch.setattr(
        factory,
        "OpenAIChatClient",
        lambda **kwargs: OpenAIChatClient(
            **kwargs, transport=httpx.MockTransport(respond)
        ),
    )
    client = factory.create_chat_client(
        Settings(
            _env_file=None,
            openai_api_key="test",
            openai_base_url="https://fixture.invalid",
            chat_model="fixture",
            chat_enable_thinking=thinking,
        )
    )
    sink = MemorySink()
    try:
        call = RecordedCall(identity(), policy(), sink) if recorded else None
        assert await client.complete("prompt", call=call) == "ok"
    finally:
        await client.close()
    expected = {
        "model": "fixture",
        "messages": [{"role": "user", "content": "prompt"}],
        "max_tokens": 2000,
    }
    config = {
        "model": "fixture",
        "max_tokens": 2000,
        "temperature": None,
        "base_url": "https://fixture.invalid",
    }
    if thinking is not None:
        expected["enable_thinking"] = thinking
        config["enable_thinking"] = thinking
    assert requests == [expected]
    if recorded:
        assert sink.rows[0].input_sha256 == canonical_hash(expected)
        assert sink.rows[0].request_config_sha256 == canonical_hash(config)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, "", "   "])
async def test_empty_final_response_reports_budget_failure_without_retry(content):
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "length", "message": {"content": content}}
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 2000},
            },
        )

    client = OpenAIChatClient("test", transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(ResponseSchemaError, match="finish_reason=length"):
            await client.complete("prompt")
        assert len(requests) == 1
        assert client.last_usage["completion_tokens"] == 2000
    finally:
        await client.close()


def test_thinking_setting_loads_from_environment(monkeypatch):
    monkeypatch.setenv("CHAT_ENABLE_THINKING", "false")
    assert Settings(_env_file=None).chat_enable_thinking is False
