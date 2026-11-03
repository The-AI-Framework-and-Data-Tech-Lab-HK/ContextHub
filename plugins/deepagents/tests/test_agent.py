"""Exercise the real Deep Agents graph, with scripted model decisions and HTTP."""

import json
from typing import Any

import pytest
from contexthub_deepagents import CONTEXTHUB_INSTRUCTIONS, create_contexthub_tools
from contexthub_sdk import ContextHubClient
from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import PrivateAttr

pytestmark = pytest.mark.asyncio

URI = "ctx://team/engineering/resources/deployment"


class ScriptedToolModel(FakeMessagesListChatModel):
    """The only stub is model inference; tool binding and graph execution are real."""

    _bound_names: list[str] = PrivateAttr(default_factory=list)
    _seen_messages: list[list[Any]] = PrivateAttr(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self._bound_names = [tool.name for tool in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._seen_messages.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def call(name, args, call_id):
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id}]
    )


async def test_deep_agent_search_read_and_feedback(client, respx_mock):
    respx_mock.post("https://hub.test/api/v1/search").respond(
        200,
        json={
            "results": [
                {
                    "uri": URI,
                    "context_type": "resource",
                    "scope": "team",
                    "score": 0.9,
                    "l1_content": "Deployment guide",
                    "status": "active",
                    "version": 2,
                }
            ],
            "total": 1,
            "retrieval_id": "retrieval-1",
        },
    )
    respx_mock.post("https://hub.test/api/v1/tools/read").respond(
        200,
        json={
            "uri": URI,
            "level": "L2",
            "content": "Deploy with release.sh after approval.",
        },
    )
    feedback = respx_mock.post("https://hub.test/api/v1/feedback").respond(
        200,
        json={
            "id": 1,
            "context_id": "d6c6a829-1b0a-4340-a2c9-13bfcc71e601",
            "retrieval_id": "retrieval-1",
            "actor": "researcher",
            "outcome": "adopted",
            "account_id": "acme",
        },
    )
    model = ScriptedToolModel(
        responses=[
            call("contexthub_search", {"query": "deployment"}, "search-1"),
            call("contexthub_read", {"uri": URI, "level": "L2"}, "read-1"),
            call(
                "contexthub_feedback",
                {
                    "context_uri": URI,
                    "outcome": "adopted",
                    "retrieval_id": "retrieval-1",
                },
                "feedback-1",
            ),
            AIMessage(content=f"Deploy with release.sh after approval. Source: {URI}"),
        ]
    )
    agent = create_deep_agent(
        model=model,
        tools=create_contexthub_tools(client),
        system_prompt=CONTEXTHUB_INSTRUCTIONS,
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "How do I deploy?"}]}
    )
    messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in messages] == ["search-1", "read-1", "feedback-1"]
    assert all(m.status == "success" for m in messages)
    assert json.loads(messages[0].content)["retrieval_id"] == "retrieval-1"
    assert "release.sh" in messages[1].content
    assert (
        json.loads(feedback.calls.last.request.content)["retrieval_id"] == "retrieval-1"
    )
    assert result["messages"][-1].content.endswith(URI)
    # Both tool families coexist; no filesystem tool was replaced by this adapter.
    assert {
        "ls",
        "grep",
        "read_file",
        "contexthub_ls",
        "contexthub_search",
        "contexthub_read",
    } <= set(model._bound_names)
    assert len(model._bound_names) == len(set(model._bound_names))
    assert any(
        isinstance(m, ToolMessage) and "release.sh" in m.content
        for m in model._seen_messages[-1]
    )


async def test_streaming_run_continues_after_forbidden_read(client, respx_mock):
    respx_mock.post("https://hub.test/api/v1/tools/read").respond(
        403, json={"detail": "Access denied"}
    )
    model = ScriptedToolModel(
        responses=[
            call("contexthub_read", {"uri": URI}, "denied-1"),
            AIMessage(content="I cannot access that source."),
        ]
    )
    agent = create_deep_agent(model=model, tools=create_contexthub_tools(client))
    states = [
        state
        async for state in agent.astream(
            {"messages": [{"role": "user", "content": "Read the deployment guide."}]},
            stream_mode="values",
        )
    ]
    tool_result = next(m for m in states[-1]["messages"] if isinstance(m, ToolMessage))
    assert tool_result.status == "error"
    assert "403" in tool_result.content
    assert states[-1]["messages"][-1].content == "I cannot access that source."
    assert any(
        isinstance(m, ToolMessage) and m.status == "error"
        for m in model._seen_messages[-1]
    )


async def test_custom_subagent_uses_its_own_contexthub_identity(client, respx_mock):
    route = respx_mock.post("https://hub.test/api/v1/search").respond(
        200,
        json={
            "results": [],
            "total": 0,
            "retrieval_id": "r-1",
        },
    )
    supervisor = ScriptedToolModel(
        responses=[
            call("contexthub_search", {"query": "my context"}, "supervisor-search"),
            call(
                "task",
                {
                    "description": "Search your shared deployment knowledge.",
                    "subagent_type": "colleague",
                },
                "delegate-1",
            ),
            AIMessage(content="The colleague completed the search."),
        ]
    )
    colleague = ScriptedToolModel(
        responses=[
            call("contexthub_search", {"query": "deployment"}, "colleague-search"),
            AIMessage(content="No matching sources were found."),
        ]
    )
    async with ContextHubClient(
        url="https://hub.test",
        api_key="test-key",
        account_id="acme",
        agent_id="colleague",
    ) as colleague_client:
        agent = create_deep_agent(
            model=supervisor,
            tools=create_contexthub_tools(client),
            subagents=[
                {
                    "name": "colleague",
                    "description": "Search shared deployment knowledge as a separate team agent.",
                    "system_prompt": CONTEXTHUB_INSTRUCTIONS,
                    "model": colleague,
                    "tools": create_contexthub_tools(colleague_client),
                }
            ],
        )
        result = await agent.ainvoke(
            {
                "messages": [
                    {"role": "user", "content": "Ask my colleague about deployment."}
                ]
            }
        )
    assert [call.request.headers["X-Agent-Id"] for call in route.calls] == [
        "researcher",
        "colleague",
    ]
    assert all(call.request.headers["X-Account-Id"] == "acme" for call in route.calls)
    assert result["messages"][-1].content == "The colleague completed the search."
