from __future__ import annotations

from contextlib import asynccontextmanager
import uuid

import pytest

from contexthub.generation.base import ContentGenerator
from contexthub.models.context import ContextLevel, ContextType, Scope
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.indexer_service import IndexerService
from contexthub.services.lifecycle_scheduler import LifecycleScheduler
from contexthub.services.lifecycle_service import LifecycleService
from contexthub.services.masking_service import MaskingService
from contexthub.store.context_store import ContextStore
from contexthub.errors import NotFoundError


class StaticEmbeddingClient:
    async def embed(self, text: str) -> list[float] | None:
        return [0.1] * 1536


class FailingEmbeddingClient:
    async def embed(self, text: str) -> list[float] | None:
        raise RuntimeError("embedding failed")


def _make_lifecycle_bundle(db_pool, embedding_client=None):
    audit = AuditService(pool=db_pool)
    indexer = None
    if embedding_client is not None:
        indexer = IndexerService(
            ContentGenerator(),
            embedding_client,
            embedding_dimensions=1536,
        )
    lifecycle = LifecycleService(audit=audit, indexer=indexer)
    store = ContextStore(
        ACLService(),
        MaskingService(),
        audit=audit,
        lifecycle=lifecycle,
    )
    return lifecycle, store, indexer


async def _insert_context(
    db,
    *,
    context_id,
    uri: str,
    account_id: str,
    context_type: str = "memory",
    scope: str = "agent",
    owner_space: str = "query-agent",
    status: str = "active",
    l0_content: str | None = "summary",
    l1_content: str | None = "detail",
    last_accessed_at=None,
    stale_at=None,
    archived_at=None,
):
    await db.execute(
        """
        INSERT INTO contexts (
            id, uri, context_type, scope, owner_space, account_id,
            l0_content, l1_content, status,
            last_accessed_at, stale_at, archived_at
        )
        VALUES (
            $1, $2, $3, $4, $5, $6,
            $7, $8, $9,
            COALESCE($10, NOW()), $11, $12
        )
        """,
        context_id,
        uri,
        context_type,
        scope,
        owner_space,
        account_id,
        l0_content,
        l1_content,
        status,
        last_accessed_at,
        stale_at,
        archived_at,
    )


async def _insert_change_event(
    db,
    *,
    context_id,
    account_id: str,
    change_type: str = "created",
    actor: str = "seed",
):
    await db.execute(
        """
        INSERT INTO change_events (context_id, account_id, change_type, actor)
        VALUES ($1, $2, $3, $4)
        """,
        context_id,
        account_id,
        change_type,
        actor,
    )


class _FakeSchedulerDb:
    def __init__(self):
        self.stale_candidates = [{"id": "missing-stale"}, {"id": "live-stale"}]
        self.archive_candidates = [{"id": "live-archive"}]
        self.delete_candidates = [{"id": "live-delete"}]

    async def fetch(self, sql, *args):
        if "FROM contexts c" not in sql:
            return []
        if "WHERE c.status = 'active'" in sql:
            return self.stale_candidates
        if "WHERE c.status = 'stale'" in sql:
            return self.archive_candidates
        if "WHERE c.status = 'archived'" in sql:
            return self.delete_candidates
        raise AssertionError(sql)


class _FakeSchedulerRepo:
    def __init__(self, db):
        self._db = db
        self.sessions: list[str] = []

    @asynccontextmanager
    async def session(self, account_id: str):
        self.sessions.append(account_id)
        yield self._db


class _FakeTenantDiscoveryConn:
    async def fetch(self, sql, *args):
        if "SELECT DISTINCT account_id" not in sql:
            raise AssertionError(sql)
        return [{"account_id": "acme"}]


class _FakeSchedulerPool:
    def __init__(self):
        self._conn = _FakeTenantDiscoveryConn()

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


class _FakeLifecycleForScheduler:
    def __init__(self):
        self.seeded_accounts: list[str] = []
        self.stale_calls: list[str] = []
        self.archive_calls: list[str] = []
        self.delete_calls: list[str] = []

    async def ensure_default_policies(self, db, ctx):
        self.seeded_accounts.append(ctx.account_id)

    async def mark_stale(self, db, context_id, reason, ctx):
        self.stale_calls.append(context_id)
        if context_id == "missing-stale":
            raise NotFoundError(f"Context {context_id} not found")

    async def mark_archived(self, db, context_id, ctx):
        self.archive_calls.append(context_id)

    async def mark_deleted(self, db, context_id, ctx):
        self.delete_calls.append(context_id)


@pytest.mark.asyncio
async def test_mark_stale_and_store_read_recover_flow(acme_session, db_pool):
    lifecycle, store, _indexer = _make_lifecycle_bundle(db_pool)
    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    context_id = uuid.uuid4()
    uri = f"ctx://agent/query-agent/memories/{context_id.hex[:8]}"

    await _insert_context(
        acme_session,
        context_id=context_id,
        uri=uri,
        account_id="acme",
    )

    await lifecycle.mark_stale(acme_session, context_id, "dependency_changed", ctx)
    stale_row = await acme_session.fetchrow(
        "SELECT status, stale_at, last_accessed_at FROM contexts WHERE id = $1",
        context_id,
    )
    assert stale_row["status"] == "stale"
    assert stale_row["stale_at"] is not None

    assert await acme_session.fetchval(
        """
        SELECT COUNT(*)
        FROM change_events
        WHERE context_id = $1 AND change_type = 'marked_stale'
        """,
        context_id,
    ) == 1
    assert await acme_session.fetchval(
        """
        SELECT COUNT(*)
        FROM audit_log
        WHERE resource_uri = $1 AND action = 'lifecycle_transition'
        """,
        uri,
    ) == 1

    before_read_events = await acme_session.fetchval("SELECT COUNT(*) FROM change_events")
    content = await store.read(acme_session, uri, ContextLevel.L1, ctx)
    after_read = await acme_session.fetchrow(
        "SELECT status, stale_at, last_accessed_at FROM contexts WHERE id = $1",
        context_id,
    )

    assert content == "detail"
    assert after_read["status"] == "active"
    assert after_read["stale_at"] is None
    assert after_read["last_accessed_at"] >= stale_row["last_accessed_at"]
    assert await acme_session.fetchval("SELECT COUNT(*) FROM change_events") == before_read_events
    assert await acme_session.fetchval(
        """
        SELECT COUNT(*)
        FROM audit_log
        WHERE resource_uri = $1 AND action = 'lifecycle_transition'
        """,
        uri,
    ) == 2


@pytest.mark.asyncio
async def test_mark_stale_is_idempotent(acme_session, db_pool):
    lifecycle, _store, _indexer = _make_lifecycle_bundle(db_pool)
    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    context_id = uuid.uuid4()
    uri = f"ctx://agent/query-agent/memories/{context_id.hex[:8]}"

    await _insert_context(
        acme_session,
        context_id=context_id,
        uri=uri,
        account_id="acme",
    )

    await lifecycle.mark_stale(acme_session, context_id, "dependency_changed", ctx)
    first_row = await acme_session.fetchrow(
        "SELECT status, stale_at FROM contexts WHERE id = $1", context_id,
    )
    assert first_row["status"] == "stale"
    first_stale_at = first_row["stale_at"]

    await lifecycle.mark_stale(acme_session, context_id, "second_call", ctx)
    second_row = await acme_session.fetchrow(
        "SELECT status, stale_at FROM contexts WHERE id = $1", context_id,
    )
    assert second_row["status"] == "stale"
    assert second_row["stale_at"] == first_stale_at

    assert await acme_session.fetchval(
        "SELECT COUNT(*) FROM change_events WHERE context_id = $1 AND change_type = 'marked_stale'",
        context_id,
    ) == 1
    assert await acme_session.fetchval(
        "SELECT COUNT(*) FROM audit_log WHERE resource_uri = $1 AND action = 'lifecycle_transition'",
        uri,
    ) == 1


@pytest.mark.asyncio
async def test_mark_archived_and_recover_from_archived_restores_embedding_and_keeps_readable(
    acme_session, db_pool
):
    lifecycle, store, indexer = _make_lifecycle_bundle(db_pool, StaticEmbeddingClient())
    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    context_id = uuid.uuid4()
    uri = f"ctx://agent/query-agent/memories/{context_id.hex[:8]}"

    await _insert_context(
        acme_session,
        context_id=context_id,
        uri=uri,
        account_id="acme",
        status="stale",
        stale_at=await acme_session.fetchval("SELECT NOW() - INTERVAL '40 days'"),
    )
    assert indexer is not None
    assert await indexer.update_embedding(acme_session, context_id, "summary") is True

    await lifecycle.mark_archived(acme_session, context_id, ctx)
    archived_row = await acme_session.fetchrow(
        """
        SELECT status, archived_at, l0_embedding IS NULL AS embedding_cleared
        FROM contexts
        WHERE id = $1
        """,
        context_id,
    )
    assert archived_row["status"] == "archived"
    assert archived_row["archived_at"] is not None
    assert archived_row["embedding_cleared"] is True

    assert await store.read(acme_session, uri, ContextLevel.L1, ctx) == "detail"

    await lifecycle.recover_from_archived(acme_session, context_id, ctx)
    restored_row = await acme_session.fetchrow(
        """
        SELECT status, archived_at, l0_embedding IS NULL AS embedding_cleared
        FROM contexts
        WHERE id = $1
        """,
        context_id,
    )
    assert restored_row["status"] == "active"
    assert restored_row["archived_at"] is None
    assert restored_row["embedding_cleared"] is False


@pytest.mark.asyncio
async def test_recover_from_archived_rolls_back_on_embedding_failure(repo, clean_db, db_pool):
    lifecycle, _store, _indexer = _make_lifecycle_bundle(db_pool, FailingEmbeddingClient())
    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    context_id = uuid.uuid4()

    async with repo.session("acme") as db:
        await _insert_context(
            db,
            context_id=context_id,
            uri=f"ctx://agent/query-agent/memories/{context_id.hex[:8]}",
            account_id="acme",
            status="archived",
            archived_at=await db.fetchval("SELECT NOW() - INTERVAL '10 days'"),
        )

    with pytest.raises(RuntimeError, match="Failed to restore embedding"):
        async with repo.session("acme") as db:
            await lifecycle.recover_from_archived(db, context_id, ctx)

    async with repo.session("acme") as db:
        row = await db.fetchrow(
            """
            SELECT status, archived_at, l0_embedding IS NULL AS embedding_cleared
            FROM contexts
            WHERE id = $1
            """,
            context_id,
        )
    assert row["status"] == "archived"
    assert row["archived_at"] is not None
    assert row["embedding_cleared"] is True


@pytest.mark.asyncio
async def test_mark_deleted_makes_context_unreadable(acme_session, db_pool):
    lifecycle, store, _indexer = _make_lifecycle_bundle(db_pool)
    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    context_id = uuid.uuid4()
    uri = f"ctx://agent/query-agent/memories/{context_id.hex[:8]}"

    await _insert_context(
        acme_session,
        context_id=context_id,
        uri=uri,
        account_id="acme",
        status="archived",
        archived_at=await acme_session.fetchval("SELECT NOW() - INTERVAL '200 days'"),
    )

    await lifecycle.mark_deleted(acme_session, context_id, ctx)
    deleted_row = await acme_session.fetchrow(
        "SELECT status, deleted_at FROM contexts WHERE id = $1",
        context_id,
    )
    assert deleted_row["status"] == "deleted"
    assert deleted_row["deleted_at"] is not None

    with pytest.raises(NotFoundError):
        await store.read(acme_session, uri, ContextLevel.L1, ctx)


@pytest.mark.asyncio
async def test_upsert_policy_updates_existing_row_and_audits(acme_session, db_pool):
    lifecycle, _store, _indexer = _make_lifecycle_bundle(db_pool)
    ctx = RequestContext(account_id="acme", agent_id="ops-agent")

    first = await lifecycle.upsert_policy(
        acme_session,
        ContextType.MEMORY,
        Scope.AGENT,
        10,
        20,
        30,
        ctx,
    )
    second = await lifecycle.upsert_policy(
        acme_session,
        "memory",
        "agent",
        1,
        2,
        3,
        ctx,
    )

    assert first.context_type == "memory"
    assert second.stale_after_days == 1
    assert second.archive_after_days == 2
    assert second.delete_after_days == 3
    assert await acme_session.fetchval(
        """
        SELECT COUNT(*)
        FROM lifecycle_policies
        WHERE context_type = 'memory' AND scope = 'agent'
        """,
    ) == 1
    assert await acme_session.fetchval(
        """
        SELECT COUNT(*)
        FROM audit_log
        WHERE action = 'policy_change'
          AND metadata->>'operation' = 'upsert_lifecycle_policy'
        """,
    ) == 2


@pytest.mark.asyncio
async def test_scheduler_run_once_cross_tenant_seeds_defaults_and_respects_zero_day_thresholds(
    repo, clean_db, db_pool
):
    lifecycle, _store, _indexer = _make_lifecycle_bundle(db_pool)
    scheduler = LifecycleScheduler(
        lifecycle=lifecycle,
        repo=repo,
        pool=db_pool,
        interval_seconds=9999,
    )
    acme_ctx = RequestContext(account_id="acme", agent_id="ops-agent")

    acme_active = uuid.uuid4()
    acme_zero_active = uuid.uuid4()
    acme_zero_stale = uuid.uuid4()
    acme_zero_archived = uuid.uuid4()
    beta_stale = uuid.uuid4()
    beta_archived = uuid.uuid4()

    async with repo.session("acme") as db:
        await lifecycle.upsert_policy(
            db,
            ContextType.MEMORY,
            Scope.AGENT,
            5,
            0,
            0,
            acme_ctx,
        )
        await _insert_context(
            db,
            context_id=acme_active,
            uri=f"ctx://agent/query-agent/memories/{acme_active.hex[:8]}",
            account_id="acme",
            last_accessed_at=await db.fetchval("SELECT NOW() - INTERVAL '10 days'"),
        )
        await _insert_context(
            db,
            context_id=acme_zero_active,
            uri=f"ctx://team/engineering/resources/{acme_zero_active.hex[:8]}",
            account_id="acme",
            context_type="resource",
            scope="team",
            owner_space="engineering",
            last_accessed_at=await db.fetchval("SELECT NOW() - INTERVAL '100 days'"),
        )
        await _insert_context(
            db,
            context_id=acme_zero_stale,
            uri=f"ctx://team/engineering/resources/{acme_zero_stale.hex[:8]}",
            account_id="acme",
            context_type="resource",
            scope="team",
            owner_space="engineering",
            status="stale",
            stale_at=await db.fetchval("SELECT NOW() - INTERVAL '100 days'"),
        )
        await _insert_change_event(db, context_id=acme_active, account_id="acme")
        await _insert_context(
            db,
            context_id=acme_zero_archived,
            uri=f"ctx://team/engineering/resources/{acme_zero_archived.hex[:8]}",
            account_id="acme",
            context_type="resource",
            scope="team",
            owner_space="engineering",
            status="archived",
            archived_at=await db.fetchval("SELECT NOW() - INTERVAL '200 days'"),
        )
        await _insert_change_event(db, context_id=acme_zero_active, account_id="acme")
        await _insert_change_event(db, context_id=acme_zero_stale, account_id="acme")
        await _insert_change_event(db, context_id=acme_zero_archived, account_id="acme")

    async with repo.session("beta") as db:
        await _insert_context(
            db,
            context_id=beta_stale,
            uri=f"ctx://agent/query-agent/memories/{beta_stale.hex[:8]}",
            account_id="beta",
            status="stale",
            stale_at=await db.fetchval("SELECT NOW() - INTERVAL '40 days'"),
        )
        await _insert_context(
            db,
            context_id=beta_archived,
            uri=f"ctx://agent/query-agent/memories/{beta_archived.hex[:8]}",
            account_id="beta",
            status="archived",
            archived_at=await db.fetchval("SELECT NOW() - INTERVAL '200 days'"),
        )
        await _insert_change_event(db, context_id=beta_stale, account_id="beta")
        await _insert_change_event(db, context_id=beta_archived, account_id="beta")

    await scheduler.run_once()

    async with repo.session("acme") as db:
        assert await db.fetchval("SELECT status FROM contexts WHERE id = $1", acme_active) == "stale"
        assert await db.fetchval("SELECT status FROM contexts WHERE id = $1", acme_zero_active) == "active"
        assert await db.fetchval("SELECT status FROM contexts WHERE id = $1", acme_zero_stale) == "stale"
        assert await db.fetchval("SELECT status FROM contexts WHERE id = $1", acme_zero_archived) == "archived"
        acme_policy = await db.fetchrow(
            """
            SELECT stale_after_days, archive_after_days, delete_after_days
            FROM lifecycle_policies
            WHERE account_id = 'acme'
              AND context_type = 'memory'
              AND scope = 'agent'
            """
        )
        assert dict(acme_policy) == {
            "stale_after_days": 5,
            "archive_after_days": 0,
            "delete_after_days": 0,
        }

    async with repo.session("beta") as db:
        assert await db.fetchval("SELECT status FROM contexts WHERE id = $1", beta_stale) == "archived"
        assert await db.fetchval("SELECT status FROM contexts WHERE id = $1", beta_archived) == "deleted"
        assert await db.fetchval(
            "SELECT COUNT(*) FROM lifecycle_policies WHERE account_id = 'beta'"
        ) == 5
        beta_policy = await db.fetchrow(
            """
            SELECT stale_after_days, archive_after_days, delete_after_days
            FROM lifecycle_policies
            WHERE account_id = 'beta'
              AND context_type = 'memory'
              AND scope = 'agent'
            """
        )
        assert dict(beta_policy) == {
            "stale_after_days": 90,
            "archive_after_days": 30,
            "delete_after_days": 180,
        }


@pytest.mark.asyncio
async def test_scheduler_ignores_missing_candidate_and_continues_other_transitions():
    lifecycle = _FakeLifecycleForScheduler()
    db = _FakeSchedulerDb()
    scheduler = LifecycleScheduler(
        lifecycle=lifecycle,
        repo=_FakeSchedulerRepo(db),
        pool=_FakeSchedulerPool(),
        interval_seconds=9999,
    )

    await scheduler.run_once()

    assert lifecycle.seeded_accounts == ["acme"]
    assert lifecycle.stale_calls == ["missing-stale", "live-stale"]
    assert lifecycle.archive_calls == ["live-archive"]
    assert lifecycle.delete_calls == ["live-delete"]
