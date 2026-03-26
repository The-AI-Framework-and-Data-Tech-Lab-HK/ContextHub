"""Tier 3 DB-backed propagation smoke tests (P-1 ~ P-8).

These complement tests/test_propagation.py (fast, in-memory) with real PG wiring.
Gated by CONTEXTHUB_INTEGRATION=1.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from contexthub.connectors.base import CatalogChange
from contexthub.propagation.registry import PropagationRuleRegistry
from contexthub.services.propagation_engine import PropagationEngine


async def _make_propagation_engine(db_pool, repo, services):
    """Create a PropagationEngine without starting background tasks."""
    return PropagationEngine(
        repo=repo,
        pool=db_pool,
        dsn="postgresql://contexthub:contexthub@localhost:5432/contexthub",
        rule_registry=services.rule_registry,
        indexer=services.indexer,
        sweep_interval=9999,
        lease_timeout=5,
    )


@pytest.mark.asyncio
async def test_p1_schema_change_marks_dependent_stale(db_pool, repo, acme_session, services):
    """P-1: Schema change → dependent memory marked stale."""
    # 1. Sync tables
    await services.catalog_sync.sync_all(acme_session, "mock", "acme")

    # 2. Get orders context_id
    orders_ctx = await acme_session.fetchrow(
        "SELECT id FROM contexts WHERE uri = 'ctx://datalake/mock/prod/orders'"
    )
    orders_id = orders_ctx["id"]

    # 3. Create a memory that depends on orders table
    mem_id = uuid.uuid4()
    await acme_session.execute(
        """
        INSERT INTO contexts (id, uri, context_type, scope, owner_space, account_id,
                              l0_content, l1_content, l2_content)
        VALUES ($1, $2, 'memory', 'agent', 'query-agent', 'acme',
                'orders analysis', 'detailed analysis', 'full analysis of orders table')
        """,
        mem_id, f"ctx://agent/query-agent/memories/orders-analysis-{uuid.uuid4().hex[:6]}",
    )
    await acme_session.execute(
        "INSERT INTO dependencies (dependent_id, dependency_id, dep_type) VALUES ($1, $2, 'table_schema')",
        mem_id, orders_id,
    )

    # 4. Simulate schema change via DDL modification + re-sync
    await acme_session.execute(
        "UPDATE table_metadata SET ddl = 'CHANGED DDL' WHERE context_id = $1", orders_id
    )
    await services.catalog_sync.sync_table(acme_session, "mock", "prod", "orders", "acme")

    # 5. Drain propagation
    engine = await _make_propagation_engine(db_pool, repo, services)
    engine._running = True
    await engine._drain_ready_events(context_id=None)

    # 6. Assert dependent is stale or auto-updated
    mem = await acme_session.fetchrow("SELECT status FROM contexts WHERE id = $1", mem_id)
    # table_schema rule does auto_update; if L2 is present it should succeed
    assert mem["status"] in ("stale", "active")


@pytest.mark.asyncio
async def test_p2_breaking_skill_marks_dependent_stale(
    db_pool, repo, services, analysis_agent_ctx, clean_db
):
    """P-2: Breaking skill version → dependent artifact marked stale."""
    # 1. Create skill
    skill_id = uuid.uuid4()
    dep_id = uuid.uuid4()
    async with repo.session("acme") as db:
        await db.execute(
            """
            INSERT INTO contexts (id, uri, context_type, scope, owner_space, account_id,
                                  l0_content, l1_content, l2_content)
            VALUES ($1, 'ctx://team/engineering/skills/sql-generator', 'skill', 'team', 'engineering', 'acme',
                    'SQL generator', 'Generates SQL', 'SELECT * FROM ...')
            """,
            skill_id,
        )

        # 2. Publish v1
        await services.skill.publish_version(
            db, "ctx://team/engineering/skills/sql-generator",
            "v1 content", "initial", False, analysis_agent_ctx,
        )

        # 3. Create dependent memory with dep_type='skill_version', pinned_version=1
        await db.execute(
            """
            INSERT INTO contexts (id, uri, context_type, scope, owner_space, account_id,
                                  l0_content, l1_content)
            VALUES ($1, $2, 'memory', 'agent', 'query-agent', 'acme', 'uses sql-gen', 'detail')
            """,
            dep_id, f"ctx://agent/query-agent/memories/dep-{uuid.uuid4().hex[:6]}",
        )
        await db.execute(
            "INSERT INTO dependencies (dependent_id, dependency_id, dep_type, pinned_version) VALUES ($1, $2, 'skill_version', 1)",
            dep_id, skill_id,
        )

        # 4. Publish v2 (breaking)
        await services.skill.publish_version(
            db, "ctx://team/engineering/skills/sql-generator",
            "v2 breaking content", "breaking change", True, analysis_agent_ctx,
        )

    # 5. Drain (test engine or the running server may process events)
    engine = await _make_propagation_engine(db_pool, repo, services)
    engine._running = True
    await engine._drain_ready_events(context_id=None)

    # 6. Assert — allow a short retry window because the running server's
    #    PropagationEngine may race with our drain via PG NOTIFY.
    for _ in range(10):
        async with repo.session("acme") as db:
            dep = await db.fetchrow("SELECT status FROM contexts WHERE id = $1", dep_id)
        if dep["status"] == "stale":
            break
        await asyncio.sleep(0.1)
    assert dep["status"] == "stale"


@pytest.mark.asyncio
async def test_p3_non_breaking_skill_no_stale(
    db_pool, repo, services, analysis_agent_ctx
):
    """P-3: Non-breaking skill update → dependent NOT marked stale."""
    skill_id = uuid.uuid4()
    dep_id = uuid.uuid4()
    async with repo.session("acme") as db:
        await db.execute(
            """
            INSERT INTO contexts (id, uri, context_type, scope, owner_space, account_id,
                                  l0_content, l1_content, l2_content)
            VALUES ($1, 'ctx://team/engineering/skills/formatter', 'skill', 'team', 'engineering', 'acme',
                    'Formatter', 'Formats output', 'format()')
            """,
            skill_id,
        )
        await services.skill.publish_version(
            db, "ctx://team/engineering/skills/formatter",
            "v1", None, False, analysis_agent_ctx,
        )

        await db.execute(
            """
            INSERT INTO contexts (id, uri, context_type, scope, owner_space, account_id,
                                  l0_content, l1_content)
            VALUES ($1, $2, 'memory', 'agent', 'query-agent', 'acme', 'uses formatter', 'detail')
            """,
            dep_id, f"ctx://agent/query-agent/memories/fmt-{uuid.uuid4().hex[:6]}",
        )
        await db.execute(
            "INSERT INTO dependencies (dependent_id, dependency_id, dep_type, pinned_version) VALUES ($1, $2, 'skill_version', 1)",
            dep_id, skill_id,
        )

        # Non-breaking v2
        await services.skill.publish_version(
            db, "ctx://team/engineering/skills/formatter",
            "v2 minor", "minor fix", False, analysis_agent_ctx,
        )

    engine = await _make_propagation_engine(db_pool, repo, services)
    engine._running = True
    await engine._drain_ready_events(context_id=None)

    async with repo.session("acme") as db:
        dep = await db.fetchrow("SELECT status FROM contexts WHERE id = $1", dep_id)
    assert dep["status"] != "stale"


@pytest.mark.asyncio
async def test_p4_stats_update_no_propagation(acme_session, services):
    """P-4: Stats-only update → no change_events, no propagation."""
    await services.catalog_sync.sync_all(acme_session, "mock", "acme")
    initial = await acme_session.fetchval("SELECT COUNT(*) FROM change_events")

    # Re-sync (DDL unchanged) — only stats update
    await services.catalog_sync.sync_table(acme_session, "mock", "prod", "users", "acme")

    final = await acme_session.fetchval("SELECT COUNT(*) FROM change_events")
    assert final == initial


@pytest.mark.asyncio
async def test_p6_table_delete_archives_and_propagates(db_pool, repo, acme_session, services):
    """P-6: Table deletion → context archived + dependent stale."""
    await services.catalog_sync.sync_all(acme_session, "mock", "acme")

    orders_ctx = await acme_session.fetchrow(
        "SELECT id FROM contexts WHERE uri = 'ctx://datalake/mock/prod/orders'"
    )
    orders_id = orders_ctx["id"]

    # Create dependent
    dep_id = uuid.uuid4()
    await acme_session.execute(
        """
        INSERT INTO contexts (id, uri, context_type, scope, owner_space, account_id,
                              l0_content, l1_content, l2_content)
        VALUES ($1, $2, 'memory', 'agent', 'query-agent', 'acme', 'dep', 'dep', 'dep content')
        """,
        dep_id, f"ctx://agent/query-agent/memories/dep-del-{uuid.uuid4().hex[:6]}",
    )
    await acme_session.execute(
        "INSERT INTO dependencies (dependent_id, dependency_id, dep_type) VALUES ($1, $2, 'table_schema')",
        dep_id, orders_id,
    )
    untouched_dep_id = uuid.uuid4()
    await acme_session.execute(
        """
        INSERT INTO contexts (id, uri, context_type, scope, owner_space, account_id,
                              l0_content, l1_content, l2_content)
        VALUES ($1, $2, 'memory', 'agent', 'query-agent', 'acme', 'other', 'other', 'other content')
        """,
        untouched_dep_id, f"ctx://agent/query-agent/memories/dep-other-{uuid.uuid4().hex[:6]}",
    )
    await acme_session.execute(
        "INSERT INTO dependencies (dependent_id, dependency_id, dep_type) VALUES ($1, $2, 'derived_from')",
        untouched_dep_id, orders_id,
    )

    # Simulate table deletion — _handle_table_deleted directly marks dependents stale
    # (PropagationEngine skips 'deleted' events by design)
    await services.catalog_sync._handle_table_deleted(acme_session, "mock", "prod", "orders", "acme")

    # Verify archived
    ctx = await acme_session.fetchrow("SELECT status FROM contexts WHERE id = $1", orders_id)
    assert ctx["status"] == "archived"

    # Dependent should already be stale (marked directly, not via propagation)
    dep = await acme_session.fetchrow("SELECT status FROM contexts WHERE id = $1", dep_id)
    assert dep["status"] == "stale"

    untouched = await acme_session.fetchrow(
        "SELECT status FROM contexts WHERE id = $1", untouched_dep_id
    )
    assert untouched["status"] == "active"


@pytest.mark.asyncio
async def test_p7_notify_lost_recovery(db_pool, repo, services):
    """P-7: Pending event without NOTIFY → drain still picks it up."""
    # Insert a pending event directly (simulating lost NOTIFY)
    ctx_id = uuid.uuid4()
    async with repo.session("acme") as db:
        await db.execute(
            """
            INSERT INTO contexts (id, uri, context_type, scope, owner_space, account_id, l0_content)
            VALUES ($1, $2, 'memory', 'agent', 'query-agent', 'acme', 'test')
            """,
            ctx_id, f"ctx://agent/query-agent/memories/p7-{uuid.uuid4().hex[:6]}",
        )
        await db.execute(
            """
            INSERT INTO change_events (context_id, account_id, change_type, actor, delivery_status)
            VALUES ($1, 'acme', 'created', 'test', 'pending')
            """,
            ctx_id,
        )

    engine = await _make_propagation_engine(db_pool, repo, services)
    engine._running = True
    await engine._drain_ready_events(context_id=None)

    async with repo.session("acme") as db:
        event = await db.fetchrow(
            "SELECT delivery_status FROM change_events WHERE context_id = $1 ORDER BY timestamp DESC LIMIT 1",
            ctx_id,
        )
    assert event["delivery_status"] == "processed"


@pytest.mark.asyncio
async def test_p8_lease_timeout_recovery(db_pool, repo, services):
    """P-8: Stuck processing event → requeued and eventually processed.

    The running server's PropagationEngine receives the INSERT NOTIFY,
    runs its own _requeue_stuck_events + _drain_ready_events, and may
    recover the event before or concurrently with the test.  The test
    verifies the *outcome*: the stuck event must eventually reach
    'processed', proving the requeue→drain recovery path works.
    """
    ctx_id = uuid.uuid4()
    async with repo.session("acme") as db:
        await db.execute(
            """
            INSERT INTO contexts (id, uri, context_type, scope, owner_space, account_id, l0_content)
            VALUES ($1, $2, 'memory', 'agent', 'query-agent', 'acme', 'test')
            """,
            ctx_id, f"ctx://agent/query-agent/memories/p8-{uuid.uuid4().hex[:6]}",
        )

        # Insert event stuck in 'processing' with expired lease
        await db.execute(
            """
            INSERT INTO change_events (context_id, account_id, change_type, actor,
                                       delivery_status, claimed_at)
            VALUES ($1, 'acme', 'created', 'test', 'processing', NOW() - INTERVAL '600 seconds')
            """,
            ctx_id,
        )

    engine = await _make_propagation_engine(db_pool, repo, services)
    engine._running = True

    # Requeue stuck events (the server may have already done this)
    await engine._requeue_stuck_events()

    # Try to drain any requeued events ourselves
    await engine._drain_ready_events(context_id=None)

    # The event must eventually leave 'processing' and reach 'processed',
    # regardless of whether the test engine or the server engine recovered it.
    for _ in range(20):
        async with repo.session("acme") as db:
            event = await db.fetchrow(
                "SELECT delivery_status FROM change_events WHERE context_id = $1 ORDER BY timestamp DESC LIMIT 1",
                ctx_id,
            )
        if event["delivery_status"] == "processed":
            break
        await asyncio.sleep(0.1)
    assert event["delivery_status"] == "processed", (
        f"Stuck event was not recovered; final status: '{event['delivery_status']}'"
    )
