"""Tests for SkillService: publish, get_versions, subscribe, read_resolved."""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from contexthub.api.routers.contexts import read_context, update_context
from contexthub.api.routers.tools import tool_read
from contexthub.api.routers.contexts import router as contexts_router
from contexthub.api.routers.tools import router as tools_router
from contexthub.errors import BadRequestError, ForbiddenError, NotFoundError
from contexthub.models.context import ContextLevel, UpdateContextRequest
from contexthub.generation.base import ContentGenerator
from contexthub.llm.base import NoOpEmbeddingClient
from contexthub.models.request import RequestContext
from contexthub.models.search import ToolReadRequest
from contexthub.models.skill import SkillContent, SkillVersionStatus
from contexthub.services.access_decision import AccessDecision
from contexthub.services.indexer_service import IndexerService
from contexthub.services.lifecycle_service import LifecycleService
from contexthub.services.masking_service import MaskingService
from contexthub.services.skill_service import SkillService


_NOW = datetime.now(timezone.utc)
_SKILL_ID = uuid.uuid4()


class FakeRecord(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)


def _make_service():
    generator = ContentGenerator()
    embedding = NoOpEmbeddingClient()
    indexer = IndexerService(generator, embedding)
    from contexthub.services.acl_service import ACLService
    acl = ACLService()
    return SkillService(indexer, acl, MaskingService())


# --- Fake DB for publish ---

class PublishDB:
    """Simulates DB for publish_version flow."""

    def __init__(self, *, skill_exists=True, is_skill=True, write_allowed=True, max_version=0):
        self._skill_exists = skill_exists
        self._is_skill = is_skill
        self._write_allowed = write_allowed
        self._max_version = max_version

    async def fetchrow(self, sql, *args):
        if "SELECT id, context_type FROM contexts WHERE uri" in sql:
            if not self._skill_exists:
                return None
            ct = "skill" if self._is_skill else "memory"
            return FakeRecord(id=_SKILL_ID, context_type=ct)
        if "SELECT scope, owner_space FROM contexts WHERE uri" in sql:
            if not self._skill_exists:
                return None
            return FakeRecord(scope="team", owner_space="engineering")
        if "FOR UPDATE" in sql:
            return FakeRecord(id=_SKILL_ID)
        raise AssertionError(f"Unexpected fetchrow: {sql}")

    async def fetchval(self, sql, *args):
        if "MAX(version)" in sql:
            return self._max_version
        if "team_memberships" in sql:
            return 1 if self._write_allowed else None
        return None

    async def fetch(self, sql, *args):
        if "visible_teams" in sql:
            return [FakeRecord(path="engineering"), FakeRecord(path="engineering/backend"), FakeRecord(path="")]
        raise AssertionError(f"Unexpected fetch: {sql}")

    async def execute(self, sql, *args):
        return "INSERT 0 1"


# --- Fake DB for get_versions ---

class GetVersionsDB:
    def __init__(self, versions):
        self._versions = versions

    async def fetchrow(self, sql, *args):
        if "SELECT id, context_type" in sql:
            return FakeRecord(id=_SKILL_ID, context_type="skill")
        if "SELECT scope, owner_space" in sql:
            return FakeRecord(scope="team", owner_space="engineering")
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        return None

    async def fetch(self, sql, *args):
        if "visible_teams" in sql:
            return [FakeRecord(path="engineering"), FakeRecord(path="engineering/backend"), FakeRecord(path="")]
        if "skill_versions" in sql:
            return self._versions
        if "access_policies" in sql:
            return []
        if "team_memberships" in sql:
            return [FakeRecord(path="engineering"), FakeRecord(path="engineering/backend")]
        raise AssertionError(sql)


# --- Fake DB for subscribe ---

class SubscribeDB:
    def __init__(self, *, pinned_version_exists=True, pinned_status="published"):
        self._pinned_version_exists = pinned_version_exists
        self._pinned_status = pinned_status

    async def fetchrow(self, sql, *args):
        if "SELECT id, context_type" in sql:
            return FakeRecord(id=_SKILL_ID, context_type="skill")
        if "SELECT scope, owner_space" in sql:
            return FakeRecord(scope="team", owner_space="engineering")
        if "SELECT status FROM skill_versions" in sql:
            if not self._pinned_version_exists:
                return None
            return FakeRecord(status=self._pinned_status)
        if "INSERT INTO skill_subscriptions" in sql:
            return FakeRecord(
                id=1, agent_id=args[0], skill_id=args[1],
                pinned_version=args[2], account_id="acme", created_at=_NOW,
            )
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        return None

    async def fetch(self, sql, *args):
        if "visible_teams" in sql:
            return [FakeRecord(path="engineering"), FakeRecord(path="engineering/backend"), FakeRecord(path="")]
        if "access_policies" in sql:
            return []
        if "team_memberships" in sql:
            return [FakeRecord(path="engineering"), FakeRecord(path="engineering/backend")]
        raise AssertionError(sql)


# --- Fake DB for read_resolved ---

class ReadResolvedDB:
    def __init__(self, *, subscription=None, versions=None, head_content="v2 content", head_version=2):
        self._subscription = subscription  # None or FakeRecord with pinned_version
        self._versions = versions or {}    # version -> FakeRecord
        self._head_content = head_content
        self._head_version = head_version

    async def fetchrow(self, sql, *args):
        if "skill_subscriptions" in sql:
            return self._subscription
        if "skill_versions" in sql and "version = $2" in sql:
            ver = args[1]
            return self._versions.get(ver)
        if "SELECT l2_content, version FROM contexts" in sql:
            return FakeRecord(l2_content=self._head_content, version=self._head_version)
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "MAX(version)" in sql:
            published = [v for v, r in self._versions.items() if r["status"] == "published"]
            return max(published) if published else None
        if "SELECT 1 FROM skill_versions" in sql:
            return 1 if any(r["status"] == "published" for r in self._versions.values()) else None
        return None


class AllowReadACL:
    async def check_read(self, db, uri, ctx):
        return True

    async def check_read_access(self, db, uri, ctx):
        return AccessDecision(allowed=True, field_masks=None, reason="test allow")


class AllowWriteACL:
    async def check_write(self, db, uri, ctx):
        return True


class RouteSkillReadDB:
    def __init__(self, status="active"):
        self.status = status
        self.last_accessed_updates = []
        self.stale_recoveries = []

    async def fetchrow(self, sql, *args):
        if "SELECT id, context_type, status" in sql and "WHERE uri" in sql:
            return FakeRecord(id=_SKILL_ID, context_type="skill", status=self.status)
        if "SELECT id, uri, status" in sql and "FROM contexts" in sql:
            return FakeRecord(
                id=_SKILL_ID,
                uri="ctx://team/engineering/skills/sql-generator",
                status=self.status,
            )
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        if "status = 'active'" in sql and "stale_at = NULL" in sql:
            self.stale_recoveries.append(args[0])
            self.status = "active"
            return "UPDATE 1"
        if "last_accessed_at" in sql:
            self.last_accessed_updates.append(args[0])
            return "UPDATE 1"
        raise AssertionError(sql)


class UnexpectedDB:
    async def fetchrow(self, sql, *args):
        raise AssertionError(sql)


class SkillPatchRouteDB:
    async def fetchrow(self, sql, *args):
        if "SELECT context_type FROM contexts WHERE uri" in sql:
            return FakeRecord(context_type="skill")
        raise AssertionError(sql)


class StubSkillService:
    async def read_resolved(self, db, skill_id, agent_id, requested_version=None):
        return SkillContent(
            content="published content",
            version=2,
            status=SkillVersionStatus.PUBLISHED,
        )


class UnexpectedUpdateService:
    async def update(self, db, uri, body, ctx):
        raise AssertionError("generic context update should not run for skills")


class _RepoSession:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeRepo:
    def __init__(self, db):
        self._db = db

    def session(self, account_id):
        return _RepoSession(self._db)


def _make_skill_http_app(db):
    app = FastAPI()
    app.include_router(contexts_router)
    app.include_router(tools_router)
    app.state.repo = FakeRepo(db)
    app.state.context_store = object()
    app.state.acl_service = AllowReadACL()
    app.state.skill_service = StubSkillService()
    app.state.masking_service = MaskingService()
    app.state.audit_service = object()
    app.state.lifecycle_service = LifecycleService()
    return app


# --- Tests ---

@pytest.mark.asyncio
async def test_publish_version_success():
    svc = _make_service()
    db = PublishDB(max_version=0)
    ctx = RequestContext(account_id="acme", agent_id="analysis-agent")

    result = await svc.publish_version(
        db, "ctx://team/engineering/skills/sql-generator",
        "SELECT * FROM ...", "Initial release", False, ctx,
    )

    assert result.version == 1
    assert result.status == SkillVersionStatus.PUBLISHED
    assert result.published_by == "analysis-agent"


@pytest.mark.asyncio
async def test_publish_version_increments():
    svc = _make_service()
    db = PublishDB(max_version=1)
    ctx = RequestContext(account_id="acme", agent_id="analysis-agent")

    result = await svc.publish_version(
        db, "ctx://team/engineering/skills/sql-generator",
        "Updated content", "Breaking change", True, ctx,
    )

    assert result.version == 2
    assert result.is_breaking is True


@pytest.mark.asyncio
async def test_publish_nonexistent_skill_returns_404():
    svc = _make_service()
    db = PublishDB(skill_exists=False)
    ctx = RequestContext(account_id="acme", agent_id="analysis-agent")

    with pytest.raises(NotFoundError):
        await svc.publish_version(db, "ctx://team/engineering/skills/nope", "c", None, False, ctx)


@pytest.mark.asyncio
async def test_publish_non_skill_returns_400():
    svc = _make_service()
    db = PublishDB(is_skill=False)
    ctx = RequestContext(account_id="acme", agent_id="analysis-agent")

    with pytest.raises(BadRequestError, match="not a skill"):
        await svc.publish_version(db, "ctx://team/engineering/skills/x", "c", None, False, ctx)


@pytest.mark.asyncio
async def test_publish_without_write_permission_returns_403():
    svc = _make_service()
    db = PublishDB(write_allowed=False)
    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    with pytest.raises(ForbiddenError):
        await svc.publish_version(
            db, "ctx://team/engineering/skills/sql-generator", "c", None, False, ctx,
        )


@pytest.mark.asyncio
async def test_get_versions_returns_published_and_deprecated():
    versions = [
        FakeRecord(
            skill_id=_SKILL_ID, version=2, content="v2", changelog="update",
            is_breaking=False, status="published", published_by="a", published_at=_NOW,
        ),
        FakeRecord(
            skill_id=_SKILL_ID, version=1, content="v1", changelog="init",
            is_breaking=False, status="published", published_by="a", published_at=_NOW,
        ),
    ]
    svc = _make_service()
    db = GetVersionsDB(versions)
    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    result = await svc.get_versions(db, "ctx://team/engineering/skills/sql-generator", ctx)

    assert len(result) == 2
    assert result[0].version == 2
    assert result[1].version == 1


@pytest.mark.asyncio
async def test_subscribe_floating():
    svc = _make_service()
    db = SubscribeDB()
    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    result = await svc.subscribe(db, "ctx://team/engineering/skills/sql-generator", None, ctx)

    assert result.agent_id == "query-agent"
    assert result.pinned_version is None


@pytest.mark.asyncio
async def test_subscribe_pinned():
    svc = _make_service()
    db = SubscribeDB(pinned_version_exists=True, pinned_status="published")
    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    result = await svc.subscribe(db, "ctx://team/engineering/skills/sql-generator", 1, ctx)

    assert result.pinned_version == 1


@pytest.mark.asyncio
async def test_subscribe_pin_nonexistent_version_returns_400():
    svc = _make_service()
    db = SubscribeDB(pinned_version_exists=False)
    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    with pytest.raises(BadRequestError, match="does not exist"):
        await svc.subscribe(db, "ctx://team/engineering/skills/sql-generator", 99, ctx)


@pytest.mark.asyncio
async def test_subscribe_pin_non_published_returns_400():
    svc = _make_service()
    db = SubscribeDB(pinned_version_exists=True, pinned_status="draft")
    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    with pytest.raises(BadRequestError, match="not published"):
        await svc.subscribe(db, "ctx://team/engineering/skills/sql-generator", 1, ctx)


@pytest.mark.asyncio
async def test_read_resolved_explicit_version():
    v1 = FakeRecord(content="v1 content", version=1, status="published")
    db = ReadResolvedDB(versions={1: v1})
    svc = _make_service()

    result = await svc.read_resolved(db, _SKILL_ID, "query-agent", requested_version=1)

    assert result.content == "v1 content"
    assert result.version == 1
    assert result.status == SkillVersionStatus.PUBLISHED


@pytest.mark.asyncio
async def test_read_resolved_explicit_deprecated_version():
    v1 = FakeRecord(content="old content", version=1, status="deprecated")
    db = ReadResolvedDB(versions={1: v1})
    svc = _make_service()

    result = await svc.read_resolved(db, _SKILL_ID, "query-agent", requested_version=1)

    assert result.status == SkillVersionStatus.DEPRECATED
    assert result.advisory is not None
    assert "deprecated" in result.advisory


@pytest.mark.asyncio
async def test_read_resolved_floating_returns_latest():
    v2 = FakeRecord(content="v2 content", version=2, status="published")
    db = ReadResolvedDB(versions={2: v2}, head_content="v2 content", head_version=2)
    svc = _make_service()

    result = await svc.read_resolved(db, _SKILL_ID, "query-agent")

    assert result.content == "v2 content"
    assert result.version == 2


@pytest.mark.asyncio
async def test_read_resolved_pinned_with_advisory():
    v1 = FakeRecord(content="v1 content", version=1, status="published")
    v2 = FakeRecord(content="v2 content", version=2, status="published")
    sub = FakeRecord(pinned_version=1)
    db = ReadResolvedDB(subscription=sub, versions={1: v1, 2: v2})
    svc = _make_service()

    result = await svc.read_resolved(db, _SKILL_ID, "query-agent")

    assert result.content == "v1 content"
    assert result.version == 1
    assert result.advisory is not None
    assert "v2" in result.advisory


@pytest.mark.asyncio
async def test_read_resolved_pinned_no_advisory_when_latest():
    v1 = FakeRecord(content="v1 content", version=1, status="published")
    sub = FakeRecord(pinned_version=1)
    db = ReadResolvedDB(subscription=sub, versions={1: v1})
    svc = _make_service()

    result = await svc.read_resolved(db, _SKILL_ID, "query-agent")

    assert result.content == "v1 content"
    assert result.advisory is None


@pytest.mark.asyncio
async def test_read_resolved_no_published_version_returns_404():
    db = ReadResolvedDB(versions={})
    svc = _make_service()

    with pytest.raises(NotFoundError, match="No published"):
        await svc.read_resolved(db, _SKILL_ID, "query-agent")


@pytest.mark.asyncio
async def test_read_resolved_explicit_draft_version_returns_404():
    """Draft versions are not accessible via read_resolved."""
    db = ReadResolvedDB(versions={})
    svc = _make_service()

    with pytest.raises(NotFoundError):
        await svc.read_resolved(db, _SKILL_ID, "query-agent", requested_version=1)


@pytest.mark.asyncio
async def test_skill_read_route_updates_last_accessed_at():
    db = RouteSkillReadDB()
    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    result = await read_context(
        uri="ctx://team/engineering/skills/sql-generator",
        level=ContextLevel.L1,
        version=None,
        ctx=ctx,
        db=db,
        store=None,
        acl=AllowReadACL(),
        skill_svc=StubSkillService(),
        masking=MaskingService(),
        audit=None,
        lifecycle=None,
    )

    assert result["version"] == 2
    assert db.last_accessed_updates == ["ctx://team/engineering/skills/sql-generator"]
    assert db.stale_recoveries == []


@pytest.mark.asyncio
async def test_skill_read_route_recovers_stale_status():
    db = RouteSkillReadDB(status="stale")
    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    result = await read_context(
        uri="ctx://team/engineering/skills/sql-generator",
        level=ContextLevel.L1,
        version=None,
        ctx=ctx,
        db=db,
        store=None,
        acl=AllowReadACL(),
        skill_svc=StubSkillService(),
        masking=MaskingService(),
        audit=None,
        lifecycle=LifecycleService(),
    )

    assert result["version"] == 2
    assert db.stale_recoveries == [_SKILL_ID]
    assert db.last_accessed_updates == []


@pytest.mark.asyncio
async def test_skill_tool_read_recovers_stale_status():
    db = RouteSkillReadDB(status="stale")
    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    result = await tool_read(
        body=ToolReadRequest(
            uri="ctx://team/engineering/skills/sql-generator",
            level=ContextLevel.L1,
        ),
        ctx=ctx,
        db=db,
        store=None,
        acl=AllowReadACL(),
        skill_svc=StubSkillService(),
        masking=MaskingService(),
        audit=None,
        lifecycle=LifecycleService(),
    )

    assert result["version"] == 2
    assert db.stale_recoveries == [_SKILL_ID]
    assert db.last_accessed_updates == []


@pytest.mark.asyncio
async def test_skill_http_read_route_recovers_stale_status():
    db = RouteSkillReadDB(status="stale")
    app = _make_skill_http_app(db)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/v1/contexts/ctx://team/engineering/skills/sql-generator",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"},
        )

    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert db.stale_recoveries == [_SKILL_ID]
    assert db.last_accessed_updates == []


@pytest.mark.asyncio
async def test_skill_http_tool_read_recovers_stale_status():
    db = RouteSkillReadDB(status="stale")
    app = _make_skill_http_app(db)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/tools/read",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"},
            json={
                "uri": "ctx://team/engineering/skills/sql-generator",
                "level": "L1",
            },
        )

    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert db.stale_recoveries == [_SKILL_ID]
    assert db.last_accessed_updates == []


@pytest.mark.asyncio
async def test_skill_read_route_rejects_user_scope_before_db_access():
    with pytest.raises(BadRequestError, match="scope=user"):
        await read_context(
            uri="ctx://user/alice/skills/private",
            level=ContextLevel.L1,
            version=None,
            ctx=RequestContext(account_id="acme", agent_id="query-agent"),
            db=UnexpectedDB(),
            store=None,
            acl=AllowReadACL(),
            skill_svc=StubSkillService(),
            masking=MaskingService(),
            audit=None,
            lifecycle=None,
        )


@pytest.mark.asyncio
async def test_skill_patch_route_is_rejected():
    with pytest.raises(BadRequestError, match="/api/v1/skills/versions"):
        await update_context(
            uri="ctx://team/engineering/skills/sql-generator",
            body=UpdateContextRequest(l2_content="mutated"),
            ctx=RequestContext(
                account_id="acme",
                agent_id="analysis-agent",
                expected_version=1,
            ),
            db=SkillPatchRouteDB(),
            svc=UnexpectedUpdateService(),
            acl=AllowWriteACL(),
        )
