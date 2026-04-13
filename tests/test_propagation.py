"""Propagation engine tests: P-1 through P-8 + event-time correctness + single-instance concurrency."""

import asyncio
import asyncpg
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

import contexthub.main as main_module
from contexthub.generation.base import ContentGenerator, GeneratedContent
from contexthub.llm.base import NoOpEmbeddingClient
from contexthub.propagation.base import PropagationAction
from contexthub.propagation.derived_memory_rule import DerivedMemoryRule
from contexthub.propagation.registry import PropagationRuleRegistry
from contexthub.propagation.skill_dep_rule import SkillVersionDepRule
from contexthub.propagation.subscription_notify_rule import SkillSubscriptionNotifyRule
from contexthub.propagation.table_schema_rule import TableSchemaRule
from contexthub.services.indexer_service import IndexerService
from contexthub.services.propagation_engine import PropagationEngine


_NOW = datetime.now(timezone.utc)
_SKILL_ID = uuid.uuid4()
_ARTIFACT_ID = uuid.uuid4()
_SOURCE_ID = uuid.uuid4()
_DEPENDENT_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Fake DB helpers
# ---------------------------------------------------------------------------

class FakeRecord(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)


class FakeScopedRepo:
    """In-memory scoped repo that records SQL calls."""

    def __init__(self, rows_by_query=None):
        self._rows = rows_by_query or {}
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.executed.append((sql, args))
        for key, rows in self._rows.items():
            if key in sql:
                return rows
        return []

    async def fetchrow(self, sql, *args):
        self.executed.append((sql, args))
        for key, rows in self._rows.items():
            if key in sql:
                return rows[0] if rows else None
        return None

    async def fetchval(self, sql, *args):
        self.executed.append((sql, args))
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


class FakePool:
    """Fake asyncpg pool that yields FakeScopedRepo as connection."""

    def __init__(self, conn=None):
        self._conn = conn or FakeScopedRepo()

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


class FakeRepo:
    """Fake PgRepository that yields FakeScopedRepo in session()."""

    def __init__(self, scoped=None):
        self._scoped = scoped or FakeScopedRepo()

    @asynccontextmanager
    async def session(self, account_id: str):
        yield self._scoped


def _make_indexer():
    generator = ContentGenerator()
    embedding = NoOpEmbeddingClient()
    return IndexerService(generator, embedding)


def _make_event(
    change_type="version_published",
    context_id=None,
    account_id="acme",
    new_version="2",
    metadata=None,
    timestamp=None,
):
    return {
        "event_id": uuid.uuid4(),
        "context_id": context_id or _SKILL_ID,
        "account_id": account_id,
        "change_type": change_type,
        "actor": "test",
        "new_version": new_version,
        "previous_version": "1",
        "metadata": metadata or {"is_breaking": True, "changelog": "breaking change"},
        "timestamp": timestamp or _NOW,
        "delivery_status": "processing",
        "attempt_count": 1,
        "diff_summary": None,
    }
def _make_engine(pool_conn=None, repo_scoped=None, indexer=None):
    pool = FakePool(pool_conn)
    repo = FakeRepo(repo_scoped)
    registry = PropagationRuleRegistry.default()
    return PropagationEngine(
        repo=repo,
        pool=pool,
        dsn="postgresql://fake",
        rule_registry=registry,
        indexer=indexer or _make_indexer(),
        sweep_interval=30,
        lease_timeout=5,
    )


# ===========================================================================
# Rule unit tests
# ===========================================================================


@pytest.mark.asyncio
async def test_skill_version_rule_breaking_marks_stale():
    """P-1 core: breaking version_published → mark_stale."""
    rule = SkillVersionDepRule()
    event = _make_event(metadata={"is_breaking": True})
    target = {"pinned_version": 1, "dependent_id": _ARTIFACT_ID}
    action = await rule.evaluate(event, target)
    assert action.action == "mark_stale"


@pytest.mark.asyncio
async def test_skill_version_rule_non_breaking_notifies():
    """P-2 core: non-breaking version_published → notify."""
    rule = SkillVersionDepRule()
    event = _make_event(metadata={"is_breaking": False})
    target = {"pinned_version": 1, "dependent_id": _ARTIFACT_ID}
    action = await rule.evaluate(event, target)
    assert action.action == "notify"


@pytest.mark.asyncio
async def test_skill_version_rule_pinned_ge_new_version_no_action():
    """P-1/P-2: artifact already on new_version → no_action."""
    rule = SkillVersionDepRule()
    event = _make_event(new_version="2", metadata={"is_breaking": True})
    target = {"pinned_version": 2, "dependent_id": _ARTIFACT_ID}
    action = await rule.evaluate(event, target)
    assert action.action == "no_action"


@pytest.mark.asyncio
async def test_skill_version_rule_no_pinned_version_no_action():
    rule = SkillVersionDepRule()
    event = _make_event()
    target = {"pinned_version": None, "dependent_id": _ARTIFACT_ID}
    action = await rule.evaluate(event, target)
    assert action.action == "no_action"


@pytest.mark.asyncio
async def test_skill_version_rule_non_publish_event_no_action():
    rule = SkillVersionDepRule()
    event = _make_event(change_type="modified")
    target = {"pinned_version": 1, "dependent_id": _ARTIFACT_ID}
    action = await rule.evaluate(event, target)
    assert action.action == "no_action"


@pytest.mark.asyncio
async def test_table_schema_rule_returns_auto_update():
    """P-3 core: table_schema → auto_update."""
    rule = TableSchemaRule()
    event = _make_event(change_type="modified")
    target = {"dependent_id": _DEPENDENT_ID}
    action = await rule.evaluate(event, target)
    assert action.action == "auto_update"


@pytest.mark.asyncio
async def test_derived_memory_rule_modified_notifies():
    """P-4 core: derived_from + modified → notify."""
    rule = DerivedMemoryRule()
    event = _make_event(change_type="modified")
    target = {"dependent_id": _DEPENDENT_ID}
    action = await rule.evaluate(event, target)
    assert action.action == "notify"


@pytest.mark.asyncio
async def test_derived_memory_rule_created_no_action():
    rule = DerivedMemoryRule()
    event = _make_event(change_type="created")
    target = {"dependent_id": _DEPENDENT_ID}
    action = await rule.evaluate(event, target)
    assert action.action == "no_action"


@pytest.mark.asyncio
async def test_subscription_notify_rule_floating():
    """P-5 core: floating subscriber → notify."""
    rule = SkillSubscriptionNotifyRule()
    event = _make_event(new_version="3")
    target = {"pinned_version": None, "agent_id": "query-agent"}
    action = await rule.evaluate(event, target)
    assert action.action == "notify"
    assert "v3" in action.reason


@pytest.mark.asyncio
async def test_subscription_notify_rule_pinned():
    """P-6 core: pinned subscriber → advisory."""
    rule = SkillSubscriptionNotifyRule()
    event = _make_event(new_version="3")
    target = {"pinned_version": 1, "agent_id": "query-agent"}
    action = await rule.evaluate(event, target)
    assert action.action == "advisory"
    assert "v1" in action.reason
# ===========================================================================
# Engine integration tests
# ===========================================================================


@pytest.mark.asyncio
async def test_p1_breaking_skill_marks_dependent_stale():
    """P-1: Full engine path — breaking skill version → dependent marked stale."""
    dep_row = FakeRecord(
        dependent_id=_ARTIFACT_ID,
        dep_type="skill_version",
        pinned_version=1,
        created_at=_NOW - timedelta(hours=1),
    )
    pool_conn = FakeScopedRepo(rows_by_query={
        "FROM dependencies": [dep_row],
    })
    scoped = FakeScopedRepo()
    engine = _make_engine(pool_conn=pool_conn, repo_scoped=scoped)

    event = _make_event(
        change_type="version_published",
        new_version="2",
        metadata={"is_breaking": True},
    )
    await engine._process_claimed_event(event)

    # Should have executed UPDATE contexts SET status='stale'
    stale_updates = [
        (sql, args) for sql, args in scoped.executed
        if "status = 'stale'" in sql
    ]
    assert len(stale_updates) >= 1

    # Should have inserted marked_stale change_event
    stale_inserts = [
        (sql, args) for sql, args in scoped.executed
        if "marked_stale" in sql and "INSERT INTO change_events" in sql
    ]
    assert len(stale_inserts) >= 1

    # Should have finished event as processed
    finish_calls = [
        (sql, args) for sql, args in pool_conn.executed
        if "delivery_status = 'processed'" in sql
    ]
    assert len(finish_calls) == 1


@pytest.mark.asyncio
async def test_p1_post_event_dependency_not_affected():
    """P-1 event-time: dependency created AFTER event → not propagated."""
    dep_row = FakeRecord(
        dependent_id=_ARTIFACT_ID,
        dep_type="skill_version",
        pinned_version=1,
        created_at=_NOW + timedelta(hours=1),  # AFTER event
    )
    pool_conn = FakeScopedRepo(rows_by_query={
        "FROM dependencies": [dep_row],
    })
    scoped = FakeScopedRepo()
    engine = _make_engine(pool_conn=pool_conn, repo_scoped=scoped)

    event = _make_event(timestamp=_NOW)

    # The fetch query filters created_at <= event.timestamp,
    # so the pool query should return nothing for this dep.
    # We simulate this by having the pool return the row but the SQL filter
    # would exclude it. Since we're using fake DB, we test the SQL contains the filter.
    await engine._process_claimed_event(event)

    # Verify the SQL used event-time filter
    dep_queries = [
        sql for sql, args in pool_conn.executed
        if "FROM dependencies" in sql
    ]
    assert any("created_at <= $2" in sql for sql in dep_queries)


@pytest.mark.asyncio
async def test_p2_non_breaking_does_not_mark_stale():
    """P-2: non-breaking version → no stale marking, only notify log."""
    dep_row = FakeRecord(
        dependent_id=_ARTIFACT_ID,
        dep_type="skill_version",
        pinned_version=1,
        created_at=_NOW - timedelta(hours=1),
    )
    pool_conn = FakeScopedRepo(rows_by_query={
        "FROM dependencies": [dep_row],
    })
    scoped = FakeScopedRepo()
    engine = _make_engine(pool_conn=pool_conn, repo_scoped=scoped)

    event = _make_event(
        change_type="version_published",
        new_version="2",
        metadata={"is_breaking": False},
    )
    await engine._process_claimed_event(event)

    # No stale updates on scoped repo
    stale_updates = [
        sql for sql, _ in scoped.executed if "status = 'stale'" in sql
    ]
    assert len(stale_updates) == 0

    # Event should be processed
    finish_calls = [
        sql for sql, _ in pool_conn.executed if "delivery_status = 'processed'" in sql
    ]
    assert len(finish_calls) == 1


@pytest.mark.asyncio
async def test_p3_table_schema_auto_update():
    """P-3: table_schema change → auto_update L0/L1, L2 unchanged."""
    dep_row = FakeRecord(
        dependent_id=_DEPENDENT_ID,
        dep_type="table_schema",
        pinned_version=None,
        created_at=_NOW - timedelta(hours=1),
    )
    source_row = FakeRecord(
        id=_SOURCE_ID,
        context_type="table_schema",
        l0_content="old l0",
        l1_content="old l1",
        l2_content="CREATE TABLE users (id INT, name TEXT)",
    )
    dependent_row = FakeRecord(
        id=_DEPENDENT_ID,
        context_type="memory",
        l2_content="Analysis of users table structure",
    )
    pool_conn = FakeScopedRepo(rows_by_query={
        "FROM dependencies": [dep_row],
    })

    # Scoped repo returns source then dependent on successive fetchrow calls
    call_count = {"n": 0}
    rows_sequence = [source_row, dependent_row]

    scoped = FakeScopedRepo()
    original_fetchrow = scoped.fetchrow

    async def ordered_fetchrow(sql, *args):
        scoped.executed.append((sql, args))
        if "FROM contexts" in sql:
            idx = call_count["n"]
            call_count["n"] += 1
            if idx < len(rows_sequence):
                return rows_sequence[idx]
        return None

    scoped.fetchrow = ordered_fetchrow

    engine = _make_engine(pool_conn=pool_conn, repo_scoped=scoped)

    event = _make_event(
        change_type="modified",
        context_id=_SOURCE_ID,
    )
    await engine._process_claimed_event(event)

    # Should have updated l0_content and l1_content
    l0l1_updates = [
        (sql, args) for sql, args in scoped.executed
        if "l0_content = $1" in sql and "l1_content = $2" in sql
    ]
    assert len(l0l1_updates) >= 1

    # Should NOT have updated l2_content
    l2_updates = [
        sql for sql, _ in scoped.executed if "l2_content" in sql and "SET" in sql
    ]
    assert len(l2_updates) == 0


@pytest.mark.asyncio
async def test_p3_auto_update_missing_source_downgrades_to_stale():
    """P-3: auto_update with missing source → downgrade to mark_stale."""
    dep_row = FakeRecord(
        dependent_id=_DEPENDENT_ID,
        dep_type="table_schema",
        pinned_version=None,
        created_at=_NOW - timedelta(hours=1),
    )
    pool_conn = FakeScopedRepo(rows_by_query={
        "FROM dependencies": [dep_row],
    })
    # scoped returns None for all fetchrow (source missing)
    scoped = FakeScopedRepo()
    engine = _make_engine(pool_conn=pool_conn, repo_scoped=scoped)

    event = _make_event(change_type="modified", context_id=_SOURCE_ID)
    await engine._process_claimed_event(event)

    # Should have fallen back to mark_stale
    stale_updates = [
        sql for sql, _ in scoped.executed if "status = 'stale'" in sql
    ]
    assert len(stale_updates) >= 1


@pytest.mark.asyncio
async def test_p3_auto_update_embedding_failure_retries_event():
    """Embedding failure should send auto_update back to retry."""
    dep_row = FakeRecord(
        dependent_id=_DEPENDENT_ID,
        dep_type="table_schema",
        pinned_version=None,
        created_at=_NOW - timedelta(hours=1),
    )
    source_row = FakeRecord(
        id=_SOURCE_ID,
        context_type="table_schema",
        l0_content="old l0",
        l1_content="old l1",
        l2_content="CREATE TABLE users (id INT, name TEXT)",
    )
    dependent_row = FakeRecord(
        id=_DEPENDENT_ID,
        context_type="memory",
        l2_content="Analysis of users table structure",
    )
    pool_conn = FakeScopedRepo(rows_by_query={
        "FROM dependencies": [dep_row],
    })

    call_count = {"n": 0}
    rows_sequence = [source_row, dependent_row]
    scoped = FakeScopedRepo()

    async def ordered_fetchrow(sql, *args):
        scoped.executed.append((sql, args))
        if "FROM contexts" in sql:
            idx = call_count["n"]
            call_count["n"] += 1
            if idx < len(rows_sequence):
                return rows_sequence[idx]
        return None

    scoped.fetchrow = ordered_fetchrow

    indexer = _make_indexer()
    indexer.update_embedding = AsyncMock(return_value=False)
    engine = _make_engine(pool_conn=pool_conn, repo_scoped=scoped, indexer=indexer)

    event = _make_event(
        change_type="modified",
        context_id=_SOURCE_ID,
    )
    await engine._process_claimed_event(event)

    retry_calls = [
        sql for sql, _ in pool_conn.executed if "delivery_status = 'retry'" in sql
    ]
    processed_calls = [
        sql for sql, _ in pool_conn.executed if "delivery_status = 'processed'" in sql
    ]
    assert len(retry_calls) == 1
    assert len(processed_calls) == 0
    indexer.update_embedding.assert_awaited_once()


@pytest.mark.asyncio
async def test_p3_auto_update_empty_l0_clears_embedding():
    """Empty regenerated L0 should clear stale vector state."""
    dep_row = FakeRecord(
        dependent_id=_DEPENDENT_ID,
        dep_type="table_schema",
        pinned_version=None,
        created_at=_NOW - timedelta(hours=1),
    )
    source_row = FakeRecord(
        id=_SOURCE_ID,
        context_type="table_schema",
        l0_content="old l0",
        l1_content="old l1",
        l2_content="CREATE TABLE users (id INT, name TEXT)",
    )
    dependent_row = FakeRecord(
        id=_DEPENDENT_ID,
        context_type="memory",
        l2_content="Analysis of users table structure",
    )
    pool_conn = FakeScopedRepo(rows_by_query={
        "FROM dependencies": [dep_row],
    })

    call_count = {"n": 0}
    rows_sequence = [source_row, dependent_row]
    scoped = FakeScopedRepo()

    async def ordered_fetchrow(sql, *args):
        scoped.executed.append((sql, args))
        if "FROM contexts" in sql:
            idx = call_count["n"]
            call_count["n"] += 1
            if idx < len(rows_sequence):
                return rows_sequence[idx]
        return None

    scoped.fetchrow = ordered_fetchrow

    indexer = _make_indexer()
    indexer.generate = AsyncMock(return_value=GeneratedContent(l0="", l1="refreshed"))
    indexer.update_embedding = AsyncMock()
    engine = _make_engine(pool_conn=pool_conn, repo_scoped=scoped, indexer=indexer)

    event = _make_event(
        change_type="modified",
        context_id=_SOURCE_ID,
    )
    await engine._process_claimed_event(event)

    clear_calls = [
        sql for sql, _ in scoped.executed if "SET l0_embedding = NULL" in sql
    ]
    processed_calls = [
        sql for sql, _ in pool_conn.executed if "delivery_status = 'processed'" in sql
    ]
    assert len(clear_calls) == 1
    assert len(processed_calls) == 1
    indexer.update_embedding.assert_not_awaited()


@pytest.mark.asyncio
async def test_p4_derived_memory_notify():
    """P-4: derived_from + modified → notify (log only), no DB mutation."""
    dep_row = FakeRecord(
        dependent_id=_DEPENDENT_ID,
        dep_type="derived_from",
        pinned_version=None,
        created_at=_NOW - timedelta(hours=1),
    )
    pool_conn = FakeScopedRepo(rows_by_query={
        "FROM dependencies": [dep_row],
    })
    scoped = FakeScopedRepo()
    engine = _make_engine(pool_conn=pool_conn, repo_scoped=scoped)

    event = _make_event(change_type="modified", context_id=_SOURCE_ID)
    await engine._process_claimed_event(event)

    # No stale or update mutations on scoped
    mutations = [
        sql for sql, _ in scoped.executed
        if "UPDATE" in sql or "INSERT" in sql
    ]
    assert len(mutations) == 0

    # Event processed
    finish_calls = [
        sql for sql, _ in pool_conn.executed if "delivery_status = 'processed'" in sql
    ]
    assert len(finish_calls) == 1


@pytest.mark.asyncio
async def test_p5_floating_subscriber_notify():
    """P-5: floating subscriber → notify log."""
    sub_row = FakeRecord(
        agent_id="query-agent",
        pinned_version=None,
        created_at=_NOW - timedelta(hours=1),
    )
    pool_conn = FakeScopedRepo(rows_by_query={
        "FROM dependencies": [],  # no deps
    })
    scoped = FakeScopedRepo(rows_by_query={
        "FROM skill_subscriptions": [sub_row],
    })
    engine = _make_engine(pool_conn=pool_conn, repo_scoped=scoped)

    event = _make_event(change_type="version_published", new_version="3")
    await engine._process_claimed_event(event)

    # Subscription query was made
    sub_queries = [
        sql for sql, _ in scoped.executed if "skill_subscriptions" in sql
    ]
    assert len(sub_queries) >= 1

    # Event processed
    finish_calls = [
        sql for sql, _ in pool_conn.executed if "delivery_status = 'processed'" in sql
    ]
    assert len(finish_calls) == 1


@pytest.mark.asyncio
async def test_p6_pinned_subscriber_advisory():
    """P-6: pinned subscriber → advisory log."""
    sub_row = FakeRecord(
        agent_id="query-agent",
        pinned_version=1,
        created_at=_NOW - timedelta(hours=1),
    )
    pool_conn = FakeScopedRepo(rows_by_query={
        "FROM dependencies": [],
    })
    scoped = FakeScopedRepo(rows_by_query={
        "FROM skill_subscriptions": [sub_row],
    })
    engine = _make_engine(pool_conn=pool_conn, repo_scoped=scoped)

    event = _make_event(change_type="version_published", new_version="3")
    await engine._process_claimed_event(event)

    # No mutations (advisory is log-only)
    mutations = [
        sql for sql, _ in scoped.executed
        if ("UPDATE" in sql and "contexts" in sql) or "INSERT" in sql
    ]
    assert len(mutations) == 0


@pytest.mark.asyncio
async def test_p7_pending_event_processed_by_drain():
    """P-7: pending event in outbox → drain picks it up and processes."""
    event = _make_event(change_type="created")
    event["delivery_status"] = "pending"

    # Pool returns the event on claim, then empty on second call
    claim_calls = {"n": 0}
    pool_conn = FakeScopedRepo()

    original_fetch = pool_conn.fetch

    async def mock_fetch(sql, *args):
        pool_conn.executed.append((sql, args))
        if "RETURNING" in sql and claim_calls["n"] == 0:
            claim_calls["n"] += 1
            return [FakeRecord(**event)]
        if "RETURNING" in sql:
            return []
        return []

    pool_conn.fetch = mock_fetch

    scoped = FakeScopedRepo(rows_by_query={"FROM dependencies": []})
    engine = _make_engine(pool_conn=pool_conn, repo_scoped=scoped)
    engine._running = True  # simulate started engine

    await engine._drain_ready_events(context_id=None)

    # Event should have been claimed and finished
    finish_calls = [
        sql for sql, _ in pool_conn.executed if "delivery_status = 'processed'" in sql
    ]
    assert len(finish_calls) >= 1


@pytest.mark.asyncio
async def test_p8_stuck_event_requeued():
    """P-8: processing event past lease_timeout → requeued to retry."""
    pool_conn = FakeScopedRepo()
    engine = _make_engine(pool_conn=pool_conn)

    await engine._requeue_stuck_events()

    requeue_calls = [
        sql for sql, _ in pool_conn.executed
        if "delivery_status = 'retry'" in sql and "processing" in sql
    ]
    assert len(requeue_calls) == 1
# ===========================================================================
# Event-time correctness tests
# ===========================================================================


@pytest.mark.asyncio
async def test_event_time_dependency_filter_in_sql():
    """Dependencies query must filter by created_at <= event.timestamp."""
    pool_conn = FakeScopedRepo()
    engine = _make_engine(pool_conn=pool_conn)

    await engine._fetch_dependents(_SKILL_ID, _NOW)

    dep_queries = [sql for sql, _ in pool_conn.executed if "dependencies" in sql]
    assert len(dep_queries) == 1
    assert "created_at <= $2" in dep_queries[0]


@pytest.mark.asyncio
async def test_event_time_subscription_filter_in_sql():
    """Subscriptions query must filter by created_at <= event.timestamp."""
    scoped = FakeScopedRepo()
    engine = _make_engine(repo_scoped=scoped)

    await engine._fetch_subscribers(_SKILL_ID, "acme", _NOW)

    sub_queries = [sql for sql, _ in scoped.executed if "skill_subscriptions" in sql]
    assert len(sub_queries) == 1
    assert "created_at <= $2" in sub_queries[0]


@pytest.mark.asyncio
async def test_marked_stale_event_no_propagation():
    """marked_stale events must not trigger further propagation (cycle prevention)."""
    pool_conn = FakeScopedRepo()
    scoped = FakeScopedRepo()
    engine = _make_engine(pool_conn=pool_conn, repo_scoped=scoped)

    event = _make_event(change_type="marked_stale")
    await engine._process_claimed_event(event)

    # No dependency or subscription queries
    dep_queries = [sql for sql, _ in pool_conn.executed if "dependencies" in sql]
    sub_queries = [sql for sql, _ in scoped.executed if "skill_subscriptions" in sql]
    assert len(dep_queries) == 0
    assert len(sub_queries) == 0

    # Event marked processed
    finish_calls = [
        sql for sql, _ in pool_conn.executed if "delivery_status = 'processed'" in sql
    ]
    assert len(finish_calls) == 1


@pytest.mark.asyncio
async def test_deleted_event_no_propagation():
    """deleted events must not trigger further propagation."""
    pool_conn = FakeScopedRepo()
    scoped = FakeScopedRepo()
    engine = _make_engine(pool_conn=pool_conn, repo_scoped=scoped)

    event = _make_event(change_type="deleted")
    await engine._process_claimed_event(event)

    dep_queries = [sql for sql, _ in pool_conn.executed if "dependencies" in sql]
    assert len(dep_queries) == 0

    finish_calls = [
        sql for sql, _ in pool_conn.executed if "delivery_status = 'processed'" in sql
    ]
    assert len(finish_calls) == 1


# ===========================================================================
# Single-instance concurrency tests
# ===========================================================================


def test_on_notify_only_sets_wakeup():
    """_on_notify must only record context_id and set wakeup, not create tasks."""
    pool_conn = FakeScopedRepo()
    engine = _make_engine(pool_conn=pool_conn)

    engine._on_notify(None, None, "context_changed", str(_SKILL_ID))
    engine._on_notify(None, None, "context_changed", str(_ARTIFACT_ID))

    assert str(_SKILL_ID) in engine._priority_context_ids
    assert str(_ARTIFACT_ID) in engine._priority_context_ids
    assert engine._wakeup.is_set()


def test_multiple_notifies_same_context_deduplicated():
    """Multiple NOTIFYs for same context_id → single entry in priority set."""
    engine = _make_engine()
    cid = str(_SKILL_ID)

    engine._on_notify(None, None, "context_changed", cid)
    engine._on_notify(None, None, "context_changed", cid)
    engine._on_notify(None, None, "context_changed", cid)

    assert len(engine._priority_context_ids) == 1


@pytest.mark.asyncio
async def test_partial_failure_retries_event():
    """If one dependency propagation fails, event goes to retry, not processed."""
    dep_row = FakeRecord(
        dependent_id=_ARTIFACT_ID,
        dep_type="skill_version",
        pinned_version=1,
        created_at=_NOW - timedelta(hours=1),
    )
    pool_conn = FakeScopedRepo(rows_by_query={
        "FROM dependencies": [dep_row],
    })

    # Make scoped repo raise on execute (simulating DB failure during mark_stale)
    scoped = FakeScopedRepo()

    async def failing_execute(sql, *args):
        scoped.executed.append((sql, args))
        raise RuntimeError("simulated DB failure")

    scoped.execute = failing_execute

    engine = _make_engine(pool_conn=pool_conn, repo_scoped=scoped)

    event = _make_event(
        change_type="version_published",
        new_version="2",
        metadata={"is_breaking": True},
    )
    await engine._process_claimed_event(event)

    # Event should be retried, not processed
    retry_calls = [
        sql for sql, _ in pool_conn.executed
        if "delivery_status = 'retry'" in sql
    ]
    assert len(retry_calls) == 1


@pytest.mark.asyncio
async def test_start_failure_closes_listener_connection():
    """Listener setup failure should not leave partial engine state behind."""
    engine = _make_engine()
    listen_conn = MagicMock()
    listen_conn.add_listener = AsyncMock(side_effect=RuntimeError("boom"))
    listen_conn.close = AsyncMock()

    with patch(
        "contexthub.services.propagation_engine.asyncpg.connect",
        AsyncMock(return_value=listen_conn),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            await engine.start()

    listen_conn.close.assert_awaited_once()
    assert engine._listen_conn is None
    assert engine._drain_task is None
    assert engine._ticker_task is None
    assert engine._running is False


@pytest.mark.asyncio
async def test_start_degrades_gracefully_when_listen_not_supported():
    """Unsupported LISTEN should fall back to polling instead of aborting startup."""
    engine = _make_engine()
    listen_conn = MagicMock()
    listen_conn.add_listener = AsyncMock(
        side_effect=asyncpg.exceptions.FeatureNotSupportedError(
            "LISTEN statement is not yet supported."
        )
    )
    listen_conn.close = AsyncMock()

    with patch(
        "contexthub.services.propagation_engine.asyncpg.connect",
        AsyncMock(return_value=listen_conn),
    ):
        await engine.start()

    listen_conn.close.assert_awaited_once()
    assert engine._listen_conn is None
    assert engine._drain_task is not None
    assert engine._ticker_task is not None
    assert engine._running is True

    await engine.stop()


@pytest.mark.asyncio
async def test_lifespan_cleans_up_when_propagation_start_fails():
    """Startup failure must still close pool and embedding client."""
    fake_pool = MagicMock()
    fake_pool.close = AsyncMock()
    fake_embedding = MagicMock()
    fake_embedding.close = AsyncMock()
    app = FastAPI()

    with patch.object(main_module, "create_pool", AsyncMock(return_value=fake_pool)), patch.object(
        main_module,
        "create_embedding_client",
        return_value=fake_embedding,
    ), patch.object(
        main_module.PropagationEngine,
        "start",
        AsyncMock(side_effect=RuntimeError("startup failed")),
    ), patch.object(
        main_module.PropagationEngine,
        "stop",
        AsyncMock(),
    ) as stop_mock:
        with pytest.raises(RuntimeError, match="startup failed"):
            async with main_module.lifespan(app):
                pass

    fake_embedding.close.assert_awaited_once()
    fake_pool.close.assert_awaited_once()
    stop_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_registry_routes_correctly():
    """Registry routes dep_type to correct rule."""
    registry = PropagationRuleRegistry.default()
    assert isinstance(registry.get_dep_rule("skill_version"), SkillVersionDepRule)
    assert isinstance(registry.get_dep_rule("table_schema"), TableSchemaRule)
    assert isinstance(registry.get_dep_rule("derived_from"), DerivedMemoryRule)
    assert registry.get_dep_rule("unknown") is None
    assert isinstance(registry.subscription_rule, SkillSubscriptionNotifyRule)
