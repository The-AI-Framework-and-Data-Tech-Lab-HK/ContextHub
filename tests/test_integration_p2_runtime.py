import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from contexthub.errors import ConflictError
from contexthub.models.request import RequestContext
from contexthub.propagation.registry import PropagationRuleRegistry
from contexthub.services.context_service import ContextService
from contexthub.services.lifecycle_service import LifecycleService
from contexthub.services.propagation_engine import PropagationEngine
from contexthub.services.propagation_engine import LeaseLostError
from contexthub.services.semantic_identity import semantic_identity
from integrations.memebench.common import wipe_account


async def _context(db, node_id, name, *, l0=None, l1=None, l2=None):
    await db.execute(
        """
        INSERT INTO contexts (
          id, uri, context_type, scope, owner_space, account_id,
          l0_content, l1_content, l2_content
        )
        VALUES ($1, $2, 'memory', 'agent', 'query-agent', 'acme', $3, $4, $5)
        """,
        node_id,
        f"ctx://agent/query-agent/memories/{name}-{node_id}",
        l0 or f"{name}-old-l0",
        l1 or f"{name}-old-l1",
        l2 or f"{name}-stable-source",
    )


@pytest.mark.asyncio
async def test_dependency_and_alias_closure_is_unserviceable_until_resolved(
    repo, clean_db
):
    root, middle, leaf, alias = (uuid4() for _ in range(4))
    lifecycle = LifecycleService()
    async with repo.session("acme") as db:
        for node, name in (
            (root, "root"),
            (middle, "middle"),
            (leaf, "leaf"),
            (alias, "alias"),
        ):
            await _context(db, node, name)
        await db.execute(
            """
            INSERT INTO dependencies (
              dependent_id, dependency_id, dep_type, dependency_version
            )
            VALUES ($1, $2, 'derived_from', 1), ($3, $1, 'derived_from', 1)
            """,
            middle,
            root,
            leaf,
        )
        await db.execute(
            """
            INSERT INTO context_relations (
              context_id, related_context_id, relation_type
            )
            VALUES ($1, $2, 'alias_of')
            """,
            alias,
            root,
        )
        source_event_id = await db.fetchval(
            """
            INSERT INTO change_events (
              context_id, account_id, change_type, actor, idempotency_key
            )
            VALUES ($1, 'acme', 'modified', 'test', $2)
            RETURNING event_id
            """,
            root,
            f"closure-root:{root}",
        )
        await lifecycle.mark_stale(
            db,
            root,
            "root changed",
            RequestContext(account_id="acme", agent_id="propagation_engine"),
            source_event={
                "event_id": source_event_id,
                "root_event_id": source_event_id,
                "depth": 0,
            },
        )
        rows = await db.fetch(
            """
            SELECT id, status, validity_status
              FROM contexts
             WHERE id = ANY($1::uuid[])
            """,
            [root, middle, leaf, alias],
        )
        validity = {row["id"]: row["validity_status"] for row in rows}
        assert validity == {
            root: "stale",
            middle: "invalid",
            leaf: "invalid",
            alias: "invalid",
        }
        assert await db.fetchval(
            "SELECT COUNT(*) FROM context_invalidations WHERE resolved_at IS NULL"
        ) == 4
        with pytest.raises(ConflictError):
            await lifecycle.recover_from_stale(
                db,
                root,
                RequestContext(account_id="acme", agent_id="query-agent"),
            )


@pytest.mark.asyncio
async def test_system_content_change_requires_exact_invalidation_resolution(
    repo, clean_db
):
    context_id = uuid4()
    other_id = uuid4()
    lifecycle = LifecycleService()
    async with repo.session("acme") as db:
        await _context(db, context_id, "guarded", l2="old semantic content")
        cause_event_id = await db.fetchval(
            """
            INSERT INTO change_events (
              context_id, account_id, change_type, actor, idempotency_key,
              source_version
            )
            VALUES ($1, 'acme', 'modified', 'test', $2, 1)
            RETURNING event_id
            """,
            context_id,
            f"guarded-cause:{context_id}",
        )
        await lifecycle.mark_stale(
            db,
            context_id,
            "dependency changed",
            RequestContext(account_id="acme", agent_id="propagation_engine"),
            source_event={
                "event_id": cause_event_id,
                "root_event_id": cause_event_id,
                "depth": 0,
                "source_version": 1,
            },
        )
        with pytest.raises(ConflictError, match="Unresolved invalidations"):
            await ContextService.apply_system_content_change(
                db,
                context_id=context_id,
                expected_version=1,
                l0_content="new",
                l1_content="new",
                l2_content="new",
                actor="test",
                diff_summary="change",
                metadata={},
                idempotency_key=f"blocked:{context_id}",
            )
        exact_causes = ((str(cause_event_id), 1),)
        with pytest.raises(ConflictError, match="semantic identity"):
            await ContextService.apply_system_content_change(
                db,
                context_id=context_id,
                expected_version=1,
                l0_content="guarded-old-l0",
                l1_content="guarded-old-l1",
                l2_content="old semantic content",
                actor="test",
                diff_summary="unchanged",
                metadata={},
                idempotency_key=f"unchanged:{context_id}",
                resolve_invalidation_causes=exact_causes,
            )
        result = await ContextService.apply_system_content_change(
            db,
            context_id=context_id,
            expected_version=1,
            l0_content="new",
            l1_content="new",
            l2_content="new semantic content",
            actor="test",
            diff_summary="legitimate recompute",
            metadata={},
            idempotency_key=f"resolved:{context_id}",
            resolve_invalidation_causes=exact_causes,
        )
        assert result["new_version"] == 2
        row = await db.fetchrow(
            """
            SELECT version, status, validity_status
              FROM contexts WHERE id = $1
            """,
            context_id,
        )
        assert dict(row) == {
            "version": 2,
            "status": "active",
            "validity_status": "fresh",
        }
        assert await db.fetchval(
            """
            SELECT COUNT(*) FROM context_invalidations
             WHERE context_id = $1 AND resolved_at IS NULL
            """,
            context_id,
        ) == 0

    async with repo.session("other-account") as db:
        await db.execute(
            """
            INSERT INTO contexts (
              id, uri, context_type, scope, owner_space, account_id,
              l0_content, l1_content, l2_content
            )
            VALUES (
              $1, $2, 'memory', 'agent', 'query-agent', 'other-account',
              'other', 'other', 'other'
            )
            """,
            other_id,
            f"ctx://agent/query-agent/memories/other-{other_id}",
        )
        with pytest.raises(ConflictError, match="version changed"):
            await ContextService.apply_system_content_change(
                db,
                context_id=other_id,
                expected_version=999,
                l0_content="bad",
                l1_content="bad",
                l2_content="bad",
                actor="test",
                diff_summary="bad cas",
                metadata={},
                idempotency_key=f"bad-cas:{other_id}",
            )
        row = await db.fetchrow(
            "SELECT version, validity_status, l2_content FROM contexts WHERE id = $1",
            other_id,
        )
        assert dict(row) == {
            "version": 1,
            "validity_status": "fresh",
            "l2_content": "other",
        }


@pytest.mark.asyncio
async def test_semantic_identity_python_and_postgres_are_byte_equivalent(
    repo, clean_db
):
    vectors = [
        (None, None, None),
        ("a  b", "c", None),
        ("a\nb", "c\td", ""),
    ]
    async with repo.session("acme") as db:
        for parts in vectors:
            actual = await db.fetchval(
                "SELECT context_semantic_identity($1, $2, $3)", *parts
            )
            assert actual == semantic_identity(*parts)


@pytest.mark.asyncio
async def test_benchmark_wipe_removes_all_runtime_children(
    repo, db_pool, clean_db
):
    async def seed(account):
        first, second = uuid4(), uuid4()
        async with repo.session(account) as db:
            for node in (first, second):
                await db.execute(
                    """
                    INSERT INTO contexts (
                      id, uri, context_type, scope, owner_space, account_id,
                      l0_content, l1_content, l2_content
                    )
                    VALUES ($1, $2, 'memory', 'agent', 'query-agent', $3,
                            'l0', 'l1', 'l2')
                    """,
                    node,
                    f"ctx://agent/query-agent/memories/{account}-{node}",
                    account,
                )
            event_id = await db.fetchval(
                """
                INSERT INTO change_events (
                  context_id, account_id, change_type, actor, idempotency_key,
                  source_version
                )
                VALUES ($1, $2, 'modified', 'test', $3, 1)
                RETURNING event_id
                """,
                first,
                account,
                f"wipe:{account}:{first}",
            )
            await db.execute(
                "INSERT INTO dependencies (dependent_id, dependency_id, dep_type) VALUES ($1, $2, 'derived_from')",
                second,
                first,
            )
            await db.execute(
                "INSERT INTO context_relations (context_id, related_context_id, relation_type) VALUES ($1, $2, 'materialized_from')",
                second,
                first,
            )
            await db.execute(
                "INSERT INTO context_feedback (context_id, retrieval_id, actor, outcome, account_id) VALUES ($1, $2, 'agent', 'adopted', $3)",
                first,
                f"retrieval-{account}",
                account,
            )
            await db.execute(
                "INSERT INTO document_sections (context_id, node_id, title, account_id) VALUES ($1, 'n1', 'section', $2)",
                first,
                account,
            )
            await db.execute(
                "INSERT INTO skill_versions (skill_id, version, content) VALUES ($1, 1, 'skill')",
                first,
            )
            await db.execute(
                "INSERT INTO skill_subscriptions (agent_id, skill_id, account_id) VALUES ($1, $2, $3)",
                f"agent-{account}",
                first,
                account,
            )
            await db.execute(
                "INSERT INTO table_metadata (context_id, catalog, database_name, table_name) VALUES ($1, 'c', 'd', 't')",
                first,
            )
            await db.execute(
                "INSERT INTO lineage (upstream_id, downstream_id) VALUES ($1, $2)",
                first,
                second,
            )
            await db.execute(
                "INSERT INTO table_relationships (table_id_a, table_id_b, join_columns) VALUES ($1, $2, '{}'::jsonb)",
                first,
                second,
            )
            await db.execute(
                "INSERT INTO query_templates (context_id, sql_template) VALUES ($1, 'SELECT 1')",
                first,
            )
            await db.execute(
                """
                INSERT INTO context_invalidations (
                  context_id, cause_event_id, source_context_id,
                  source_version, reason_hash
                ) VALUES ($1, $2, $3, 1, 'reason')
                """,
                second,
                event_id,
                first,
            )
            await db.execute(
                """
                INSERT INTO propagation_effects (
                  event_id, effect_key, effect_type, target_context_id, status
                ) VALUES ($1, 'edge', 'dependency', $2, 'succeeded')
                """,
                event_id,
                second,
            )
            await db.execute(
                "INSERT INTO propagation_risk_ledger (event_id, edge_key) VALUES ($1, 'edge')",
                event_id,
            )
            await db.execute(
                "INSERT INTO propagation_trace (event_id, trace_type, payload) VALUES ($1, 'test', '{}'::jsonb)",
                event_id,
            )
            await db.execute(
                """
                INSERT INTO retrieval_trace (
                  retrieval_id, account_id, agent_id, request_hash,
                  candidates, final_versions, context_hash
                )
                VALUES ($1, $2, 'agent', 'r', '[]', '[]', 'c')
                """,
                uuid4(),
                account,
            )
        return first

    await seed("wipe-me")
    keep_context = await seed("keep-me")
    await wipe_account(SimpleNamespace(pool=db_pool), "wipe-me")
    async with db_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM contexts WHERE account_id = 'wipe-me'"
        ) == 0
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM change_events WHERE account_id = 'wipe-me'"
        ) == 0
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM retrieval_trace WHERE account_id = 'wipe-me'"
        ) == 0
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM contexts WHERE account_id = 'keep-me'"
        ) == 2
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM context_feedback WHERE account_id = 'keep-me'"
        ) == 1
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM document_sections WHERE account_id = 'keep-me'"
        ) == 1
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM skill_subscriptions WHERE account_id = 'keep-me'"
        ) == 1
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM skill_versions WHERE skill_id = $1",
            keep_context,
        ) == 1


@pytest.mark.asyncio
async def test_old_worker_cannot_finish_effect_after_event_takeover(
    repo, db_pool, clean_db
):
    context_id = uuid4()
    old_token, new_token = uuid4(), uuid4()
    async with repo.session("acme") as db:
        await _context(db, context_id, "effect-fence")
        event_id = await db.fetchval(
            """
            INSERT INTO change_events (
              context_id, account_id, change_type, actor, idempotency_key,
              delivery_status, lease_token
            )
            VALUES ($1, 'acme', 'modified', 'test', $2, 'processing', $3)
            RETURNING event_id
            """,
            context_id,
            f"effect-fence:{context_id}",
            old_token,
        )
        await db.execute(
            """
            INSERT INTO propagation_effects (
              event_id, effect_key, effect_type, status, lease_token
            )
            VALUES ($1, 'edge', 'dependency', 'started', $2)
            """,
            event_id,
            old_token,
        )
        await db.execute(
            "UPDATE change_events SET lease_token = $2 WHERE event_id = $1",
            event_id,
            new_token,
        )
    runtime = PropagationEngine(
        repo,
        db_pool,
        "postgresql://unused",
        PropagationRuleRegistry.default(),
        LifecycleService(),
    )
    with pytest.raises(LeaseLostError):
        await runtime._finish_effect(
            {"event_id": event_id, "lease_token": old_token},
            "edge",
            succeeded=True,
            result={"late": True},
        )
    async with repo.session("acme") as db:
        assert await db.fetchval(
            """
            SELECT status FROM propagation_effects
             WHERE event_id = $1 AND effect_key = 'edge'
            """,
            event_id,
        ) == "started"


@pytest.mark.asyncio
async def test_old_worker_cannot_apply_business_effect_after_event_takeover(
    repo, db_pool, clean_db, action
):
    source, dependent = uuid4(), uuid4()
    old_token, new_token = uuid4(), uuid4()
    dep_type = "derived_from" if action == "mark_stale" else "table_schema"
    effect_key = f"dependency:{dep_type}:{dependent}:1"
    async with repo.session("acme") as db:
        await _context(db, source, "takeover-source")
        await _context(db, dependent, "takeover-dependent")
        event_id = await db.fetchval(
            """
            INSERT INTO change_events (
              context_id, account_id, change_type, actor, idempotency_key,
              source_version, delivery_status, lease_token
            )
            VALUES ($1, 'acme', 'modified', 'test', $2, 1, 'processing', $3)
            RETURNING event_id
            """,
            source,
            f"side-effect-takeover:{action}:{source}",
            old_token,
        )
        await db.execute(
            """
            INSERT INTO propagation_effects (
              event_id, effect_key, effect_type, target_context_id,
              source_version, status, lease_token
            )
            VALUES ($1, $2, 'dependency', $3, 1, 'started', $4)
            """,
            event_id,
            effect_key,
            dependent,
            old_token,
        )
        before = dict(
            await db.fetchrow(
                """
                SELECT status, validity_status, version, l0_content, l1_content,
                       l2_content
                  FROM contexts WHERE id = $1
                """,
                dependent,
            )
        )
        child_events_before = await db.fetchval(
            "SELECT COUNT(*) FROM change_events WHERE context_id = $1",
            dependent,
        )
        await db.execute(
            "UPDATE change_events SET lease_token = $2 WHERE event_id = $1",
            event_id,
            new_token,
        )

    runtime = PropagationEngine(
        repo,
        db_pool,
        "postgresql://unused",
        PropagationRuleRegistry.default(),
        LifecycleService(),
    )
    stale_event = {
        "event_id": event_id,
        "context_id": source,
        "account_id": "acme",
        "change_type": "modified",
        "source_version": 1,
        "lease_token": old_token,
        "metadata": {},
    }
    with pytest.raises(LeaseLostError):
        await runtime._mark_stale(
            dependent,
            stale_event,
            "SIDE_EFFECT_AFTER_LEASE_LOSS",
            effect_key=effect_key,
            effect_result={"action": "mark_stale"},
        )

    async with repo.session("acme") as db:
        after = dict(
            await db.fetchrow(
                """
                SELECT status, validity_status, version, l0_content, l1_content,
                       l2_content
                  FROM contexts WHERE id = $1
                """,
                dependent,
            )
        )
        assert after == before
        assert await db.fetchval(
            "SELECT COUNT(*) FROM change_events WHERE context_id = $1",
            dependent,
        ) == child_events_before
        effect = await db.fetchrow(
            """
            SELECT status, lease_token FROM propagation_effects
             WHERE event_id = $1 AND effect_key = $2
            """,
            event_id,
            effect_key,
        )
        assert effect["status"] == "started"
        assert effect["lease_token"] == old_token
