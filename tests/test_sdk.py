"""Unit tests for ContextHub SDK.

Uses respx to mock HTTP without starting the server or database.
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
import respx

from contexthub_sdk import (
    AuthenticationError,
    BadRequestError,
    ConflictError,
    ContextHubClient,
    ContextLevel,
    ContextReadResult,
    ContextRecord,
    ContextStat,
    ContextStatus,
    ContextType,
    DependencyRecord,
    ForbiddenError,
    MemoryRecord,
    NotFoundError,
    PreconditionRequiredError,
    ResolvedSkillReadResult,
    Scope,
    SearchResponse,
    ServerError,
    SkillVersionStatus,
)

BASE = "https://hub.test"
API_KEY = "test-key"
ACCOUNT = "acct-1"
AGENT = "agent-1"
TS = "2026-03-25T00:00:00Z"

CTX_ID = str(uuid4())
SKILL_ID = str(uuid4())
CTX_URI = "ctx://team/team-a/schema/orders"
SKILL_URI = "ctx://team/team-a/skills/retrieval"
MEMORY_URI = f"ctx://agent/{AGENT}/memories/mem-1234abcd"
PROMOTED_MEMORY_URI = "ctx://team/team-a/memories/shared_knowledge/mem-1234abcd"

CTX_RECORD = {
    "id": CTX_ID,
    "uri": CTX_URI,
    "context_type": "table_schema",
    "scope": "team",
    "owner_space": "team-a",
    "account_id": ACCOUNT,
    "l0_content": "orders schema",
    "l1_content": "orders table summary",
    "l2_content": "CREATE TABLE orders (...);",
    "file_path": None,
    "status": "active",
    "version": 1,
    "tags": ["orders"],
    "created_at": TS,
    "updated_at": TS,
    "last_accessed_at": TS,
    "stale_at": None,
    "archived_at": None,
    "deleted_at": None,
    "active_count": 0,
    "adopted_count": 0,
    "ignored_count": 0,
}

CTX_STAT = {
    "id": CTX_ID,
    "uri": CTX_URI,
    "context_type": "table_schema",
    "scope": "team",
    "owner_space": "team-a",
    "status": "active",
    "version": 1,
    "tags": ["orders"],
    "active_count": 3,
    "adopted_count": 1,
    "ignored_count": 0,
    "created_at": TS,
    "updated_at": TS,
    "last_accessed_at": TS,
}

DEPENDENCY = {
    "dep_type": "derived_from",
    "pinned_version": None,
    "dependent_uri": CTX_URI,
    "dependency_uri": "ctx://datalake/raw/orders",
}

MEMORY_CONTEXT_RECORD = {
    "id": str(uuid4()),
    "uri": MEMORY_URI,
    "context_type": "memory",
    "scope": "agent",
    "owner_space": AGENT,
    "account_id": ACCOUNT,
    "l0_content": "remember this",
    "l1_content": "remember this",
    "l2_content": "remember this",
    "file_path": None,
    "status": "active",
    "version": 1,
    "tags": [],
    "created_at": TS,
    "updated_at": TS,
    "last_accessed_at": TS,
    "stale_at": None,
    "archived_at": None,
    "deleted_at": None,
    "active_count": 0,
    "adopted_count": 0,
    "ignored_count": 0,
}

PROMOTED_MEMORY_RECORD = {
    **MEMORY_CONTEXT_RECORD,
    "id": str(uuid4()),
    "uri": PROMOTED_MEMORY_URI,
    "scope": "team",
    "owner_space": "team-a",
}

MEMORY_LIST_RECORD = {
    "uri": MEMORY_URI,
    "l0_content": "remember this",
    "status": "active",
    "version": 1,
    "tags": [],
    "created_at": TS,
    "updated_at": TS,
}

SKILL_VERSION = {
    "skill_id": SKILL_ID,
    "version": 1,
    "content": "skill content",
    "changelog": None,
    "is_breaking": False,
    "status": "published",
    "published_by": AGENT,
    "published_at": TS,
}

SKILL_SUB = {
    "id": 1,
    "agent_id": AGENT,
    "skill_id": SKILL_ID,
    "pinned_version": None,
    "account_id": ACCOUNT,
    "created_at": TS,
}


@pytest.fixture
def client():
    return ContextHubClient(
        url=BASE,
        api_key=API_KEY,
        account_id=ACCOUNT,
        agent_id=AGENT,
    )


@respx.mock
@pytest.mark.asyncio
async def test_headers_injected(client: ContextHubClient):
    route = respx.post(f"{BASE}/api/v1/search").mock(
        return_value=httpx.Response(200, json={"results": [], "total": 0})
    )
    await client.search("test")
    req = route.calls.last.request
    assert req.headers["x-api-key"] == API_KEY
    assert req.headers["x-account-id"] == ACCOUNT
    assert req.headers["x-agent-id"] == AGENT


@respx.mock
@pytest.mark.asyncio
async def test_search_returns_typed_and_uses_server_enum_values(client: ContextHubClient):
    route = respx.post(f"{BASE}/api/v1/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "uri": CTX_URI,
                        "context_type": "table_schema",
                        "scope": "team",
                        "owner_space": "team-a",
                        "score": 0.9,
                        "l0_content": "orders schema",
                        "l1_content": "orders table summary",
                        "l2_content": None,
                        "status": "active",
                        "version": 1,
                        "tags": ["orders"],
                    }
                ],
                "total": 1,
            },
        )
    )
    resp = await client.search(
        "orders",
        scope=[Scope.TEAM],
        context_type=[ContextType.TABLE_SCHEMA],
    )
    body = json.loads(route.calls.last.request.content)
    assert body["scope"] == ["team"]
    assert body["context_type"] == ["table_schema"]
    assert isinstance(resp, SearchResponse)
    assert resp.total == 1
    assert resp.results[0].context_type == ContextType.TABLE_SCHEMA
    assert resp.results[0].status == ContextStatus.ACTIVE


@respx.mock
@pytest.mark.asyncio
async def test_context_create(client: ContextHubClient):
    route = respx.post(f"{BASE}/api/v1/contexts").mock(
        return_value=httpx.Response(201, json=CTX_RECORD)
    )
    rec = await client.context.create(
        uri=CTX_URI,
        context_type=ContextType.TABLE_SCHEMA,
        scope=Scope.TEAM,
        owner_space="team-a",
        l1_content="orders table summary",
    )
    body = json.loads(route.calls.last.request.content)
    assert body["context_type"] == "table_schema"
    assert body["scope"] == "team"
    assert body["owner_space"] == "team-a"
    assert isinstance(rec, ContextRecord)
    assert rec.uri == CTX_URI


@respx.mock
@pytest.mark.asyncio
async def test_context_read_non_skill(client: ContextHubClient):
    respx.get(f"{BASE}/api/v1/contexts/{CTX_URI}").mock(
        return_value=httpx.Response(
            200,
            json={"uri": CTX_URI, "level": "L1", "content": "orders table summary"},
        )
    )
    result = await client.context.read(CTX_URI)
    assert isinstance(result, ContextReadResult)
    assert result.level == ContextLevel.L1
    assert result.content == "orders table summary"


@respx.mock
@pytest.mark.asyncio
async def test_context_read_skill(client: ContextHubClient):
    respx.get(f"{BASE}/api/v1/contexts/{SKILL_URI}").mock(
        return_value=httpx.Response(
            200,
            json={
                "uri": SKILL_URI,
                "version": 3,
                "content": "skill body",
                "status": "published",
                "advisory": None,
            },
        )
    )
    result = await client.context.read(SKILL_URI)
    assert isinstance(result, ResolvedSkillReadResult)
    assert result.version == 3
    assert result.status == SkillVersionStatus.PUBLISHED


@respx.mock
@pytest.mark.asyncio
async def test_context_stat_returns_typed(client: ContextHubClient):
    respx.get(f"{BASE}/api/v1/contexts/{CTX_URI}/stat").mock(
        return_value=httpx.Response(200, json=CTX_STAT)
    )
    result = await client.context.stat(CTX_URI)
    assert isinstance(result, ContextStat)
    assert result.context_type == ContextType.TABLE_SCHEMA
    assert result.tags == ["orders"]


@respx.mock
@pytest.mark.asyncio
async def test_context_children_returns_strings(client: ContextHubClient):
    respx.get(f"{BASE}/api/v1/contexts/{CTX_URI}/children").mock(
        return_value=httpx.Response(200, json=["columns", "indexes"])
    )
    result = await client.context.children(CTX_URI)
    assert result == ["columns", "indexes"]


@respx.mock
@pytest.mark.asyncio
async def test_context_deps_returns_typed(client: ContextHubClient):
    respx.get(f"{BASE}/api/v1/contexts/{CTX_URI}/deps").mock(
        return_value=httpx.Response(200, json=[DEPENDENCY])
    )
    result = await client.context.deps(CTX_URI)
    assert isinstance(result[0], DependencyRecord)
    assert result[0].dependency_uri == "ctx://datalake/raw/orders"


@respx.mock
@pytest.mark.asyncio
async def test_context_update_sends_if_match(client: ContextHubClient):
    route = respx.patch(f"{BASE}/api/v1/contexts/{CTX_URI}").mock(
        return_value=httpx.Response(200, json={**CTX_RECORD, "version": 2, "status": "stale"})
    )
    rec = await client.context.update(
        CTX_URI,
        expected_version=1,
        status=ContextStatus.STALE,
    )
    body = json.loads(route.calls.last.request.content)
    assert route.calls.last.request.headers["if-match"] == "1"
    assert body["status"] == "stale"
    assert rec.status == ContextStatus.STALE


@respx.mock
@pytest.mark.asyncio
async def test_context_delete_sends_if_match(client: ContextHubClient):
    route = respx.delete(f"{BASE}/api/v1/contexts/{CTX_URI}").mock(
        return_value=httpx.Response(204)
    )
    await client.context.delete(CTX_URI, expected_version=1)
    assert route.calls.last.request.headers["if-match"] == "1"


@respx.mock
@pytest.mark.asyncio
async def test_memory_add_returns_context_record(client: ContextHubClient):
    route = respx.post(f"{BASE}/api/v1/memories").mock(
        return_value=httpx.Response(201, json=MEMORY_CONTEXT_RECORD)
    )
    rec = await client.memory.add(content="remember this")
    body = json.loads(route.calls.last.request.content)
    assert body["content"] == "remember this"
    assert isinstance(rec, ContextRecord)
    assert rec.context_type == ContextType.MEMORY
    assert rec.l2_content == "remember this"


@respx.mock
@pytest.mark.asyncio
async def test_memory_list_returns_typed_summary(client: ContextHubClient):
    respx.get(f"{BASE}/api/v1/memories").mock(
        return_value=httpx.Response(200, json=[MEMORY_LIST_RECORD])
    )
    mems = await client.memory.list()
    assert isinstance(mems[0], MemoryRecord)
    assert mems[0].l0_content == "remember this"
    assert mems[0].status == ContextStatus.ACTIVE


@respx.mock
@pytest.mark.asyncio
async def test_memory_promote_returns_context_record(client: ContextHubClient):
    route = respx.post(f"{BASE}/api/v1/memories/promote").mock(
        return_value=httpx.Response(201, json=PROMOTED_MEMORY_RECORD)
    )
    rec = await client.memory.promote(uri=MEMORY_URI, target_team="team-a")
    body = json.loads(route.calls.last.request.content)
    assert body["target_team"] == "team-a"
    assert isinstance(rec, ContextRecord)
    assert rec.scope == Scope.TEAM
    assert rec.uri == PROMOTED_MEMORY_URI


@respx.mock
@pytest.mark.asyncio
async def test_skill_publish(client: ContextHubClient):
    route = respx.post(f"{BASE}/api/v1/skills/versions").mock(
        return_value=httpx.Response(201, json=SKILL_VERSION)
    )
    rec = await client.skill.publish(skill_uri=SKILL_URI, content="skill content")
    body = json.loads(route.calls.last.request.content)
    assert body["skill_uri"] == SKILL_URI
    assert body["is_breaking"] is False
    assert rec.version == 1
    assert rec.status == SkillVersionStatus.PUBLISHED


@respx.mock
@pytest.mark.asyncio
async def test_skill_versions(client: ContextHubClient):
    respx.get(f"{BASE}/api/v1/skills/{SKILL_URI}/versions").mock(
        return_value=httpx.Response(200, json=[SKILL_VERSION])
    )
    versions = await client.skill.versions(SKILL_URI)
    assert versions[0].status == SkillVersionStatus.PUBLISHED


@respx.mock
@pytest.mark.asyncio
async def test_skill_subscribe(client: ContextHubClient):
    route = respx.post(f"{BASE}/api/v1/skills/subscribe").mock(
        return_value=httpx.Response(200, json=SKILL_SUB)
    )
    sub = await client.skill.subscribe(skill_uri=SKILL_URI)
    body = json.loads(route.calls.last.request.content)
    assert body["skill_uri"] == SKILL_URI
    assert sub.agent_id == AGENT


@respx.mock
@pytest.mark.asyncio
async def test_tools_ls(client: ContextHubClient):
    route = respx.post(f"{BASE}/api/v1/tools/ls").mock(
        return_value=httpx.Response(200, json=["schema", "skills"])
    )
    result = await client.ls("ctx://team/team-a")
    body = json.loads(route.calls.last.request.content)
    assert body["path"] == "ctx://team/team-a"
    assert result == ["schema", "skills"]


@respx.mock
@pytest.mark.asyncio
async def test_tools_read(client: ContextHubClient):
    respx.post(f"{BASE}/api/v1/tools/read").mock(
        return_value=httpx.Response(
            200,
            json={"uri": CTX_URI, "level": "L1", "content": "orders table summary"},
        )
    )
    result = await client.read(CTX_URI)
    assert isinstance(result, ContextReadResult)
    assert result.level == ContextLevel.L1


@respx.mock
@pytest.mark.asyncio
async def test_tools_grep(client: ContextHubClient):
    respx.post(f"{BASE}/api/v1/tools/grep").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "uri": CTX_URI,
                        "context_type": "table_schema",
                        "scope": "team",
                        "owner_space": "team-a",
                        "score": 0.8,
                        "l0_content": "orders schema",
                        "l1_content": "orders table summary",
                        "l2_content": None,
                        "status": "active",
                        "version": 1,
                        "tags": ["orders"],
                    }
                ],
                "total": 1,
            },
        )
    )
    resp = await client.grep("orders")
    assert isinstance(resp, SearchResponse)
    assert resp.results[0].context_type == ContextType.TABLE_SCHEMA


@respx.mock
@pytest.mark.asyncio
async def test_tools_stat(client: ContextHubClient):
    respx.post(f"{BASE}/api/v1/tools/stat").mock(
        return_value=httpx.Response(200, json=CTX_STAT)
    )
    result = await client.stat(CTX_URI)
    assert isinstance(result, ContextStat)
    assert result.version == 1


@respx.mock
@pytest.mark.asyncio
async def test_tools_read_skill_polymorphic(client: ContextHubClient):
    respx.post(f"{BASE}/api/v1/tools/read").mock(
        return_value=httpx.Response(
            200,
            json={
                "uri": SKILL_URI,
                "version": 2,
                "content": "skill body",
                "status": "published",
                "advisory": "v3 available, currently pinned to v2",
            },
        )
    )
    result = await client.read(SKILL_URI)
    assert isinstance(result, ResolvedSkillReadResult)
    assert result.advisory == "v3 available, currently pinned to v2"


@respx.mock
@pytest.mark.asyncio
async def test_401_maps_to_authentication_error(client: ContextHubClient):
    respx.post(f"{BASE}/api/v1/search").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid API key"})
    )
    with pytest.raises(AuthenticationError):
        await client.search("q")


@respx.mock
@pytest.mark.asyncio
async def test_428_maps_to_precondition_required(client: ContextHubClient):
    respx.patch(f"{BASE}/api/v1/contexts/{CTX_URI}").mock(
        return_value=httpx.Response(428, json={"detail": "If-Match header required"})
    )
    with pytest.raises(PreconditionRequiredError):
        await client.context.update(CTX_URI, expected_version=1, l1_content="x")


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,exc_cls",
    [
        (400, BadRequestError),
        (403, ForbiddenError),
        (404, NotFoundError),
        (409, ConflictError),
        (500, ServerError),
        (502, ServerError),
    ],
)
async def test_error_code_mapping(client: ContextHubClient, status, exc_cls):
    respx.post(f"{BASE}/api/v1/search").mock(
        return_value=httpx.Response(status, json={"detail": "err"})
    )
    with pytest.raises(exc_cls):
        await client.search("q")


@respx.mock
@pytest.mark.asyncio
async def test_async_with_closes_client():
    respx.post(f"{BASE}/api/v1/search").mock(
        return_value=httpx.Response(200, json={"results": [], "total": 0})
    )
    async with ContextHubClient(
        url=BASE,
        api_key=API_KEY,
        account_id=ACCOUNT,
        agent_id=AGENT,
    ) as sdk_client:
        await sdk_client.search("q")
    assert sdk_client._http.is_closed
