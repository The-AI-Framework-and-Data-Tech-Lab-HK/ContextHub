from __future__ import annotations

import respx
from httpx import Response
import pytest

from contexthub.llm.openai_client import OpenAIEmbeddingClient


@pytest.mark.asyncio
@respx.mock
async def test_embed_pads_shorter_embeddings_to_expected_dimensions():
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

    assert embedding == [0.25, -0.5, 0.0, 0.0]


@pytest.mark.asyncio
@respx.mock
async def test_embed_batch_pads_shorter_embeddings_to_expected_dimensions():
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

    assert embeddings == [[1.0, 0.0, 0.0], [2.0, 3.0, 0.0]]


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
