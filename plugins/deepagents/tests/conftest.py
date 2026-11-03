"""Tests exercise the real SDK without a database or model-provider account."""

import pytest
import pytest_asyncio
from contexthub_deepagents import create_contexthub_tools
from contexthub_sdk import ContextHubClient


@pytest_asyncio.fixture
async def client():
    async with ContextHubClient(
        url="https://hub.test",
        api_key="test-key",
        account_id="acme",
        agent_id="researcher",
    ) as value:
        yield value


@pytest.fixture
def tools(client):
    return {tool.name: tool for tool in create_contexthub_tools(client)}


@pytest.fixture(autouse=True)
def disable_tracing(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
