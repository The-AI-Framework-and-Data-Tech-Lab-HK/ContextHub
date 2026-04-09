"""Tests for MemoryService: add, list, promote, and ACL negative cases."""

import uuid
from datetime import datetime, timezone

import pytest

from contexthub.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from contexthub.generation.base import ContentGenerator
from contexthub.llm.base import NoOpEmbeddingClient
from contexthub.models.context import Scope
from contexthub.models.memory import AddMemoryRequest, PromoteRequest
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService
from contexthub.services.indexer_service import IndexerService
from contexthub.services.masking_service import MaskingService
from contexthub.services.memory_service import MemoryService


# --- Fake DB helpers ---

_NOW = datetime.now(timezone.utc)
_FAKE_ID = uuid.uuid4()


class FakeRecord(dict):
    """Dict that also supports attribute access (like asyncpg.Record)."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)


def _make_memory_row(
    uri="ctx://agent/query-agent/memories/mem-abc12345",
    context_type="memory",
    scope="agent",
    owner_space="query-agent",
    account_id="acme",
    l2_content="Some memory content",
    **overrides,
):
    base = {
        "id": overrides.get("id", _FAKE_ID),
        "uri": uri,
        "context_type": context_type,
        "scope": scope,
        "owner_space": owner_space,
        "account_id": account_id,
        "l0_content": overrides.get("l0_content", "Some memory"),
        "l1_content": overrides.get("l1_content", "Some memory content"),
        "l2_content": l2_content,
        "file_path": None,
        "status": "active",
        "version": 1,
        "tags": overrides.get("tags", []),
        "created_at": _NOW,
        "updated_at": _NOW,
        "last_accessed_at": _NOW,
        "stale_at": None,
        "archived_at": None,
        "deleted_at": None,
        "active_count": 0,
        "adopted_count": 0,
        "ignored_count": 0,
    }
    base.update(overrides)
    return FakeRecord(base)


# --- Fake DB for add_memory ---

class AddMemoryDB:
    """Simulates DB for add_memory: INSERT returns a row."""

    def __init__(self):
        self.inserted = []

    async def fetchrow(self, sql, *args):
        if "INSERT INTO contexts" in sql:
            row = _make_memory_row(uri=args[0], owner_space=args[1], tags=list(args[5] or []))
            self.inserted.append(row)
            return row
        raise AssertionError(f"Unexpected fetchrow: {sql}")

    async def execute(self, sql, *args):
        return "INSERT 0 1"


# --- Fake DB for list_memories ---

class ListMemoriesDB:
    """Returns a set of memories for listing, plus visible team paths."""

    async def fetch(self, sql, *args):
        if "visible_teams" in sql:
            return [FakeRecord(path="engineering/backend"), FakeRecord(path="engineering"), FakeRecord(path="")]
        if "context_type = 'memory'" in sql:
            assert "scope IN ('agent', 'team')" in sql
            return [
                FakeRecord(
                    uri="ctx://agent/query-agent/memories/mem-abc",
                    l0_content="My memory",
                    status="active",
                    version=1,
                    tags=[],
                    created_at=_NOW,
                    updated_at=_NOW,
                    scope="agent",
                    owner_space="query-agent",
                ),
                FakeRecord(
                    uri="ctx://agent/other-agent/memories/mem-xyz",
                    l0_content="Other memory",
                    status="active",
                    version=1,
                    tags=[],
                    created_at=_NOW,
                    updated_at=_NOW,
                    scope="agent",
                    owner_space="other-agent",
                ),
                FakeRecord(
                    uri="ctx://team/engineering/backend/memories/shared_knowledge/mem-shared",
                    l0_content="Shared memory",
                    status="active",
                    version=1,
                    tags=[],
                    created_at=_NOW,
                    updated_at=_NOW,
                    scope="team",
                    owner_space="engineering/backend",
                ),
            ]
        if "access_policies" in sql:
            return []
        if "team_memberships" in sql:
            return [FakeRecord(path="engineering/backend"), FakeRecord(path="engineering")]
        raise AssertionError(f"Unexpected fetch: {sql}")


# --- Fake DB for promote ---

class PromoteDB:
    """Simulates DB for promote flow."""

    def __init__(self, source_row, *, write_allowed=True, duplicate=False):
        self._source = source_row
        self._write_allowed = write_allowed
        self._duplicate = duplicate
        self.deps_inserted = []
        self.events_inserted = []
        self._promoted_id = uuid.uuid4()

    async def fetchrow(self, sql, *args):
        if "SELECT * FROM contexts WHERE uri" in sql:
            return self._source
        if "INSERT INTO contexts" in sql:
            if self._duplicate:
                raise Exception("duplicate key value violates unique constraint")
            return _make_memory_row(
                id=self._promoted_id,
                uri=args[0],
                scope="team",
                owner_space=args[1],
                l2_content=self._source["l2_content"] if self._source else "",
            )
        raise AssertionError(f"Unexpected fetchrow: {sql}")

    async def fetchval(self, sql, *args):
        if "team_memberships" in sql:
            return 1 if self._write_allowed else None
        return None

    async def fetch(self, sql, *args):
        if "visible_teams" in sql:
            return [
                FakeRecord(path="engineering/backend"),
                FakeRecord(path="engineering"),
                FakeRecord(path=""),
            ]
        raise AssertionError(f"Unexpected fetch: {sql}")

    async def execute(self, sql, *args):
        if "INSERT INTO dependencies" in sql:
            self.deps_inserted.append(args)
        elif "INSERT INTO change_events" in sql:
            self.events_inserted.append(args)
        return "INSERT 0 1"


def _make_service():
    generator = ContentGenerator()
    embedding = NoOpEmbeddingClient()
    indexer = IndexerService(generator, embedding)
    acl = ACLService()
    return MemoryService(indexer, acl, MaskingService())


# --- Tests ---

@pytest.mark.asyncio
async def test_add_memory_creates_private_memory():
    svc = _make_service()
    db = AddMemoryDB()
    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    body = AddMemoryRequest(content="SELECT * FROM orders is slow", tags=["perf"])

    result = await svc.add_memory(db, body, ctx)

    assert result.uri.startswith("ctx://agent/query-agent/memories/")
    assert result.context_type == "memory"
    assert result.scope == "agent"
    assert result.owner_space == "query-agent"


@pytest.mark.asyncio
async def test_list_memories_filters_by_visibility():
    svc = _make_service()
    db = ListMemoriesDB()
    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    memories = await svc.list_memories(db, ctx)

    uris = [m["uri"] for m in memories]
    # query-agent should see own memory and team memory, not other-agent's
    assert "ctx://agent/query-agent/memories/mem-abc" in uris
    assert "ctx://team/engineering/backend/memories/shared_knowledge/mem-shared" in uris
    assert "ctx://agent/other-agent/memories/mem-xyz" not in uris


@pytest.mark.asyncio
async def test_promote_to_team_with_write_permission():
    source = _make_memory_row()
    db = PromoteDB(source, write_allowed=True)
    svc = _make_service()
    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    body = PromoteRequest(uri=source["uri"], target_team="engineering/backend")

    result = await svc.promote(db, body, ctx)

    assert result.uri.startswith("ctx://team/engineering/backend/memories/shared_knowledge/")
    assert result.scope == "team"
    assert result.owner_space == "engineering/backend"
    # derived_from dependency was inserted
    assert len(db.deps_inserted) == 1
    assert db.deps_inserted[0][1] == source["id"]  # dependency_id = source
    # change event was inserted
    assert len(db.events_inserted) == 1


@pytest.mark.asyncio
async def test_promote_to_team_without_write_permission():
    """query-agent cannot write to 'engineering' (no direct read_write membership)."""
    source = _make_memory_row()
    db = PromoteDB(source, write_allowed=False)
    svc = _make_service()
    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    body = PromoteRequest(uri=source["uri"], target_team="engineering")

    with pytest.raises(ForbiddenError):
        await svc.promote(db, body, ctx)


@pytest.mark.asyncio
async def test_promote_not_own_memory_returns_403():
    """Cannot promote another agent's memory."""
    source = _make_memory_row(owner_space="other-agent")
    db = PromoteDB(source, write_allowed=True)
    svc = _make_service()
    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    body = PromoteRequest(uri=source["uri"], target_team="engineering/backend")

    with pytest.raises(ForbiddenError, match="own private"):
        await svc.promote(db, body, ctx)


@pytest.mark.asyncio
async def test_promote_non_memory_returns_400():
    source = _make_memory_row(context_type="skill")
    db = PromoteDB(source, write_allowed=True)
    svc = _make_service()
    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    body = PromoteRequest(uri=source["uri"], target_team="engineering/backend")

    with pytest.raises(BadRequestError, match="Only memory"):
        await svc.promote(db, body, ctx)


@pytest.mark.asyncio
async def test_promote_nonexistent_returns_404():
    db = PromoteDB(None, write_allowed=True)
    svc = _make_service()
    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    body = PromoteRequest(uri="ctx://agent/query-agent/memories/nope", target_team="engineering/backend")

    with pytest.raises(NotFoundError):
        await svc.promote(db, body, ctx)


@pytest.mark.asyncio
async def test_promote_duplicate_returns_409():
    source = _make_memory_row()
    db = PromoteDB(source, write_allowed=True, duplicate=True)
    svc = _make_service()
    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    body = PromoteRequest(uri=source["uri"], target_team="engineering/backend")

    with pytest.raises(ConflictError):
        await svc.promote(db, body, ctx)


@pytest.mark.asyncio
async def test_promote_to_root_team_uri_format():
    """Promote to root team (empty string) produces correct URI."""
    source = _make_memory_row()

    class RootTeamDB(PromoteDB):
        async def fetch(self, sql, *args):
            if "visible_teams" in sql:
                return [FakeRecord(path="")]
            raise AssertionError(sql)

        async def fetchval(self, sql, *args):
            if "team_memberships" in sql:
                return 1
            return None

    db = RootTeamDB(source, write_allowed=True)
    svc = _make_service()
    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    body = PromoteRequest(uri=source["uri"], target_team="")

    result = await svc.promote(db, body, ctx)
    assert result.uri.startswith("ctx://team/memories/shared_knowledge/")
