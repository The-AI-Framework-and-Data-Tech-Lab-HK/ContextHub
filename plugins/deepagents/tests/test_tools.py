"""SDK/HTTP contracts, JSON serialization, validation, and identity boundaries."""

import asyncio
import json

import httpx
import pytest
from contexthub_deepagents import create_contexthub_tools
from contexthub_sdk import ContextHubClient
from langchain_core.messages import ToolMessage
from pydantic import ValidationError

pytestmark = pytest.mark.asyncio

URI = "ctx://team/engineering/resources/deployment"
CONTEXT_ID = "d6c6a829-1b0a-4340-a2c9-13bfcc71e601"
MEMORY_URI = "ctx://agent/researcher/memories/deployment"
MEMORY = {
    "id": CONTEXT_ID,
    "uri": MEMORY_URI,
    "context_type": "memory",
    "scope": "agent",
    "account_id": "acme",
}


async def test_search_preserves_evidence_and_stale_notices(tools, respx_mock):
    route = respx_mock.post("https://hub.test/api/v1/search").respond(
        200,
        json={
            "results": [
                {
                    "uri": URI,
                    "context_type": "resource",
                    "scope": "team",
                    "score": 0.9,
                    "status": "active",
                    "version": 3,
                    "snippet": "Deploy with the release script.",
                    "section_id": 42,
                }
            ],
            "total": 1,
            "retrieval_id": "retrieval-1",
            "stale_notices": [
                {
                    "uri": URI + "-old",
                    "validity_status": "stale",
                    "reason": "procedure changed",
                    "stale_content": "Run the retired script.",
                }
            ],
        },
    )
    message = await tools["contexthub_search"].ainvoke(
        {
            "type": "tool_call",
            "id": "search-call",
            "name": "contexthub_search",
            "args": {
                "query": "deployment",
                "scope": ["team"],
                "context_type": ["resource"],
                "top_k": 3,
                "level": "L2",
                "include_stale_notices": True,
            },
        }
    )
    assert isinstance(message, ToolMessage)
    assert message.status == "success"
    assert message.tool_call_id == "search-call"
    result = json.loads(message.content)
    assert result["retrieval_id"] == "retrieval-1"
    assert result["results"][0]["version"] == 3
    assert result["results"][0]["section_id"] == 42
    assert result["results"][0]["uri"] == URI
    assert result["stale_notices"][0]["reason"] == "procedure changed"
    request = route.calls.last.request
    assert json.loads(request.content) == {
        "query": "deployment",
        "scope": ["team"],
        "context_type": ["resource"],
        "top_k": 3,
        "level": "L2",
        "include_stale": False,
        "include_stale_notices": True,
    }
    assert request.headers["X-Account-Id"] == "acme"
    assert request.headers["X-Agent-Id"] == "researcher"
    assert request.headers["X-API-Key"] == "test-key"


@pytest.mark.parametrize(
    "name,args,path,response,status,body",
    [
        (
            "contexthub_ls",
            {"path": "ctx://team/engineering"},
            "/tools/ls",
            [URI],
            200,
            {"path": "ctx://team/engineering"},
        ),
        (
            "contexthub_read",
            {"uri": URI, "level": "L2"},
            "/tools/read",
            {"uri": URI, "level": "L2", "content": "Full original text."},
            200,
            {"uri": URI, "level": "L2"},
        ),
        (
            "contexthub_read",
            {"uri": "ctx://team/engineering/skills/deploy", "version": 2},
            "/tools/read",
            {
                "uri": "ctx://team/engineering/skills/deploy",
                "version": 2,
                "content": "Pinned skill.",
                "status": "published",
                "advisory": "v3 available",
            },
            200,
            {
                "uri": "ctx://team/engineering/skills/deploy",
                "level": "L1",
                "version": 2,
            },
        ),
        (
            "contexthub_stat",
            {"uri": URI},
            "/tools/stat",
            {
                "id": CONTEXT_ID,
                "uri": URI,
                "context_type": "resource",
                "scope": "team",
                "status": "active",
                "version": 3,
                "active_count": 1,
                "adopted_count": 1,
                "ignored_count": 0,
            },
            200,
            {"uri": URI},
        ),
        (
            "contexthub_store",
            {"content": "Use the release script.", "tags": ["deploy"]},
            "/memories",
            MEMORY,
            201,
            {"content": "Use the release script.", "tags": ["deploy"]},
        ),
        (
            "contexthub_promote",
            {"uri": MEMORY_URI, "target_team": "engineering"},
            "/memories/promote",
            {**MEMORY, "scope": "team", "uri": URI},
            201,
            {"uri": MEMORY_URI, "target_team": "engineering"},
        ),
        (
            "contexthub_skill_publish",
            {
                "skill_uri": URI,
                "content": "New procedure.",
                "changelog": "New API",
                "is_breaking": True,
            },
            "/skills/versions",
            {
                "skill_id": CONTEXT_ID,
                "version": 4,
                "content": "New procedure.",
                "status": "published",
                "is_breaking": True,
            },
            201,
            {
                "skill_uri": URI,
                "content": "New procedure.",
                "changelog": "New API",
                "is_breaking": True,
            },
        ),
        (
            "contexthub_feedback",
            {"context_uri": URI, "outcome": "adopted", "retrieval_id": "retrieval-1"},
            "/feedback",
            {
                "id": 1,
                "context_id": CONTEXT_ID,
                "retrieval_id": "retrieval-1",
                "actor": "researcher",
                "outcome": "adopted",
                "account_id": "acme",
            },
            200,
            {"context_uri": URI, "outcome": "adopted", "retrieval_id": "retrieval-1"},
        ),
        (
            "contexthub_feedback",
            {"context_uri": URI, "outcome": "irrelevant"},
            "/feedback",
            {
                "id": 2,
                "context_id": CONTEXT_ID,
                "retrieval_id": "degraded-1",
                "actor": "researcher",
                "outcome": "irrelevant",
                "account_id": "acme",
            },
            200,
            {"context_uri": URI, "outcome": "irrelevant"},
        ),
    ],
)
async def test_sdk_http_operations(
    tools, respx_mock, name, args, path, response, status, body
):
    route = respx_mock.post("https://hub.test/api/v1" + path).respond(
        status, json=response
    )
    result = await tools[name].ainvoke(args)
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content) == body
    if isinstance(response, list):
        assert result == response
    else:
        # SDK defaults can add fields; every returned server field survives JSON serialization.
        assert {key: result[key] for key in response} == response
    json.dumps(result)


async def test_document_navigation(tools, respx_mock):
    prefix = f"https://hub.test/api/v1/documents/{CONTEXT_ID}"
    respx_mock.get(prefix + "/sections").respond(
        200,
        json=[
            {
                "section_id": 42,
                "title": "Deployment",
                "depth": 1,
                "start_offset": 10,
                "end_offset": 30,
            }
        ],
    )
    respx_mock.get(prefix + "/section/42").respond(
        200,
        json={
            "context_id": CONTEXT_ID,
            "section_id": 42,
            "title": "Deployment",
            "content": "Original section text.",
            "start_offset": 10,
            "end_offset": 30,
        },
    )
    sections = await tools["contexthub_sections"].ainvoke({"context_id": CONTEXT_ID})
    result = await tools["contexthub_read_section"].ainvoke(
        {
            "context_id": CONTEXT_ID,
            "section_id": sections[0]["section_id"],
        }
    )
    assert result["context_id"] == CONTEXT_ID
    assert result["content"] == "Original section text."
    assert result["start_offset"] == 10


@pytest.mark.parametrize("status", [401, 403, 404, 409, 500])
async def test_http_errors_are_error_messages(tools, respx_mock, status):
    respx_mock.post("https://hub.test/api/v1/tools/read").respond(
        status, json={"detail": "Denied or unavailable"}
    )
    result = await tools["contexthub_read"].ainvoke(
        {
            "type": "tool_call",
            "id": "read-call",
            "name": "contexthub_read",
            "args": {"uri": URI},
        }
    )
    assert result.status == "error"
    assert str(status) in result.content
    assert "Denied or unavailable" in result.content


async def test_timeout_is_an_error_without_exposing_transport_details(
    tools, respx_mock
):
    route = respx_mock.post("https://hub.test/api/v1/search").mock(
        side_effect=httpx.ReadTimeout("private connection details"),
    )
    result = await tools["contexthub_search"].ainvoke(
        {
            "type": "tool_call",
            "id": "search-call",
            "name": "contexthub_search",
            "args": {"query": "test"},
        }
    )
    assert result.status == "error"
    assert "private connection details" not in result.content
    assert route.call_count == 1


@pytest.mark.parametrize(
    "args",
    [
        {"query": "test", "top_k": 0},
        {"query": "test", "top_k": 101},
        {"query": "test", "scope": ["invalid"]},
        {"query": "test", "context_type": ["invalid"]},
        {"query": "test", "level": "L3"},
    ],
)
async def test_invalid_search_arguments_never_reach_http(tools, respx_mock, args):
    with pytest.raises(ValidationError):
        await tools["contexthub_search"].ainvoke(args)
    assert not respx_mock.calls


async def test_invalid_server_contract_is_not_hidden_as_empty_results(
    tools, respx_mock
):
    respx_mock.post("https://hub.test/api/v1/search").respond(
        200, json={"results": [], "total": 0}
    )
    with pytest.raises(ValidationError):
        await tools["contexthub_search"].ainvoke({"query": "test"})


async def test_parallel_agents_keep_their_bound_identities(client, respx_mock):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200, json={"results": [], "total": 0, "retrieval_id": "r-1"}
        )

    respx_mock.post("https://hub.test/api/v1/search").mock(side_effect=respond)
    async with ContextHubClient(
        url="https://hub.test",
        api_key="other-key",
        account_id="other-account",
        agent_id="other-agent",
    ) as other:
        first_tools = create_contexthub_tools(client)
        second_tools = create_contexthub_tools(other)
        first = next(t for t in first_tools if t.name == "contexthub_search")
        second = next(t for t in second_tools if t.name == "contexthub_search")
        for tool in first_tools + second_tools:
            assert not {"api_key", "account_id", "agent_id"} & tool.args.keys()
        await asyncio.gather(
            first.ainvoke({"query": "first"}),
            second.ainvoke({"query": "second"}),
        )
        assert {
            (r.headers["X-Account-Id"], r.headers["X-Agent-Id"], r.headers["X-API-Key"])
            for r in requests
        } == {
            ("acme", "researcher", "test-key"),
            ("other-account", "other-agent", "other-key"),
        }
        # The adapter must not close a client owned by the calling application.
        await first.ainvoke({"query": "next turn"})
