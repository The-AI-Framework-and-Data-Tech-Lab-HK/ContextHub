from __future__ import annotations

import respx
from httpx import Response
import pytest

from contexthub.llm.openai_client import OpenAIEmbeddingClient


@pytest.mark.asyncio
@respx.mock
async def test_embed_rejects_shorter_embeddings_without_padding():
    respx.post("https://api.example.com/v1/embeddings").mock(
        return_value=Response(
            200,
            json={
                "data": [
                    {
                        "embedding": [0.25, -0.5],
                        "index": 0,
                    }
                ]
            },
        )
    )
    client = OpenAIEmbeddingClient(
        api_key="sk-test",
        base_url="https://api.example.com/v1",
        model="test-embedding",
        expected_dimensions=4,
    )

    try:
        embedding = await client.embed("hello")
    finally:
        await client.close()

    assert embedding is None


@pytest.mark.asyncio
@respx.mock
async def test_embed_batch_rejects_shorter_embeddings_without_padding():
    respx.post("https://api.example.com/v1/embeddings").mock(
        return_value=Response(
            200,
            json={
                "data": [
                    {"embedding": [1.0], "index": 0},
                    {"embedding": [2.0, 3.0], "index": 1},
                ]
            },
        )
    )
    client = OpenAIEmbeddingClient(
        api_key="sk-test",
        base_url="https://api.example.com/v1",
        model="test-embedding",
        expected_dimensions=3,
    )

    try:
        embeddings = await client.embed_batch(["a", "b"])
    finally:
        await client.close()

    assert embeddings == [None, None]


@pytest.mark.asyncio
@respx.mock
async def test_embed_rejects_embeddings_larger_than_expected_dimensions():
    respx.post("https://api.example.com/v1/embeddings").mock(
        return_value=Response(
            200,
            json={
                "data": [
                    {
                        "embedding": [0.1, 0.2, 0.3],
                        "index": 0,
                    }
                ]
            },
        )
    )
    client = OpenAIEmbeddingClient(
        api_key="sk-test",
        base_url="https://api.example.com/v1",
        model="test-embedding",
        expected_dimensions=2,
    )

    try:
        embedding = await client.embed("hello")
    finally:
        await client.close()

    assert embedding is None


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
@respx.mock
async def test_factory_sends_configured_qwen_model_and_dimensions(batch):
    import json
    from contexthub.config import Settings
    from contexthub.llm.factory import create_embedding_client

    settings = Settings(_env_file=None, openai_api_key="test-key",
                        openai_base_url="https://api.example.com/v1",
                        embedding_model="qwen3.7-text-embedding", embedding_dimensions=1536)
    first = [1.0] + [0.0] * 1535
    second = [0.0, 1.0] + [0.0] * 1534

    def response(request):
        payload = json.loads(request.content)
        assert payload == {"model": "qwen3.7-text-embedding", "dimensions": 1536,
                           "input": ["first", "second"] if batch else "first"}
        data = [{"embedding": first, "index": 0}]
        if batch:
            data.insert(0, {"embedding": second, "index": 1})
        return Response(200, json={"data": data})

    route = respx.post("https://api.example.com/v1/embeddings").mock(side_effect=response)
    client = create_embedding_client(settings)
    try:
        result = await client.embed_batch(["first", "second"]) if batch else await client.embed("first")
    finally:
        await client.close()
    assert result == ([first, second] if batch else first)
    assert route.call_count == 1
