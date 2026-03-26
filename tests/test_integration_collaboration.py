"""Tier 3 multi-Agent collaboration tests (C-1 ~ C-5).

Gated by CONTEXTHUB_INTEGRATION=1.
"""

import uuid

import pytest
import pytest_asyncio

from contexthub.models.memory import AddMemoryRequest, PromoteRequest


@pytest.mark.asyncio
async def test_c1_memory_promote(acme_session, services, query_agent_ctx):
    """C-1: Private memory → promote to team → derived_from dependency created."""
    # Create private memory
    body = AddMemoryRequest(content="Orders table uses user_id as FK to users", tags=["schema"])
    mem = await services.memory.add_memory(acme_session, body, query_agent_ctx)
    assert mem.scope.value == "agent"

    # Promote
    promote_body = PromoteRequest(uri=mem.uri, target_team="engineering")
    promoted = await services.memory.promote(acme_session, promote_body, query_agent_ctx)
    assert promoted.scope.value == "team"
    assert "engineering" in promoted.uri

    # Verify derived_from dependency
    dep = await acme_session.fetchrow(
        "SELECT * FROM dependencies WHERE dependent_id = $1 AND dep_type = 'derived_from'",
        promoted.id,
    )
    assert dep is not None
    assert dep["dependency_id"] == mem.id


@pytest.mark.asyncio
async def test_c2_promoted_visible_to_other_agent(acme_session, services, query_agent_ctx, analysis_agent_ctx):
    """C-2: Promoted memory visible to analysis-agent via team search."""
    body = AddMemoryRequest(content="Important: always use LEFT JOIN for orders", tags=["sql"])
    mem = await services.memory.add_memory(acme_session, body, query_agent_ctx)
    promote_body = PromoteRequest(uri=mem.uri, target_team="engineering")
    await services.memory.promote(acme_session, promote_body, query_agent_ctx)

    # analysis-agent lists memories — should see promoted
    memories = await services.memory.list_memories(acme_session, analysis_agent_ctx)
    uris = [m["uri"] for m in memories]
    promoted_uris = [u for u in uris if "shared_knowledge" in u]
    assert len(promoted_uris) >= 1


@pytest.mark.asyncio
async def test_c3_source_change_propagation(db_pool, repo, acme_session, services, query_agent_ctx):
    """C-3: Source memory change → derived_from notify (log only, no auto-modify)."""
    body = AddMemoryRequest(content="Schema note: orders has 5 columns", tags=["schema"])
    mem = await services.memory.add_memory(acme_session, body, query_agent_ctx)
    promote_body = PromoteRequest(uri=mem.uri, target_team="engineering")
    promoted = await services.memory.promote(acme_session, promote_body, query_agent_ctx)

    # Modify source memory's l2_content + insert change_event
    await acme_session.execute(
        "UPDATE contexts SET l2_content = 'Updated: orders now has 6 columns', version = version + 1 WHERE id = $1",
        mem.id,
    )
    await acme_session.execute(
        """
        INSERT INTO change_events (context_id, account_id, change_type, actor, diff_summary)
        VALUES ($1, 'acme', 'modified', 'query-agent', 'updated schema note')
        """,
        mem.id,
    )

    # Drain propagation
    from contexthub.services.propagation_engine import PropagationEngine
    engine = PropagationEngine(
        repo=repo, pool=db_pool,
        dsn="postgresql://contexthub:contexthub@localhost:5432/contexthub",
        rule_registry=services.rule_registry, indexer=services.indexer,
        sweep_interval=9999, lease_timeout=5,
    )
    engine._running = True
    await engine._drain_ready_events(context_id=None)

    # Promoted memory should NOT be auto-modified (derived_from → notify only)
    p = await acme_session.fetchrow("SELECT l2_content, status FROM contexts WHERE id = $1", promoted.id)
    assert p["l2_content"] == mem.l2_content  # unchanged
    assert p["status"] == "active"


@pytest.mark.asyncio
async def test_c4_skill_pinned_subscription(acme_session, services, query_agent_ctx, analysis_agent_ctx):
    """C-4: Pinned subscription reads fixed version, advisory on newer."""
    skill_id = uuid.uuid4()
    await acme_session.execute(
        """
        INSERT INTO contexts (id, uri, context_type, scope, owner_space, account_id,
                              l0_content, l1_content, l2_content)
        VALUES ($1, 'ctx://team/engineering/skills/sql-gen-c4', 'skill', 'team', 'engineering', 'acme',
                'SQL gen', 'Generates SQL', 'SELECT 1')
        """,
        skill_id,
    )

    # Publish v1, v2
    await services.skill.publish_version(
        acme_session, "ctx://team/engineering/skills/sql-gen-c4",
        "v1 content", "initial", False, query_agent_ctx,
    )
    await services.skill.publish_version(
        acme_session, "ctx://team/engineering/skills/sql-gen-c4",
        "v2 content", "update", False, query_agent_ctx,
    )

    # analysis-agent subscribes pinned to v2
    await services.skill.subscribe(
        acme_session, "ctx://team/engineering/skills/sql-gen-c4", 2, analysis_agent_ctx,
    )

    # Publish v3
    await services.skill.publish_version(
        acme_session, "ctx://team/engineering/skills/sql-gen-c4",
        "v3 content", "another update", False, query_agent_ctx,
    )

    # read_resolved should return v2 with advisory
    resolved = await services.skill.read_resolved(acme_session, skill_id, "analysis-agent")
    assert resolved.version == 2
    assert resolved.content == "v2 content"
    assert resolved.advisory is not None
    assert "v3" in resolved.advisory


@pytest.mark.asyncio
async def test_c5_skill_floating_subscription(acme_session, services, query_agent_ctx):
    """C-5: Floating subscription always reads latest."""
    skill_id = uuid.uuid4()
    await acme_session.execute(
        """
        INSERT INTO contexts (id, uri, context_type, scope, owner_space, account_id,
                              l0_content, l1_content, l2_content)
        VALUES ($1, 'ctx://team/engineering/skills/sql-gen-c5', 'skill', 'team', 'engineering', 'acme',
                'SQL gen', 'Generates SQL', 'SELECT 1')
        """,
        skill_id,
    )

    await services.skill.publish_version(
        acme_session, "ctx://team/engineering/skills/sql-gen-c5",
        "v1", None, False, query_agent_ctx,
    )

    # Floating subscription (pinned_version=None)
    await services.skill.subscribe(
        acme_session, "ctx://team/engineering/skills/sql-gen-c5", None, query_agent_ctx,
    )

    # Publish v2, v3, v4
    for v in range(2, 5):
        await services.skill.publish_version(
            acme_session, "ctx://team/engineering/skills/sql-gen-c5",
            f"v{v} content", None, False, query_agent_ctx,
        )

    resolved = await services.skill.read_resolved(acme_session, skill_id, "query-agent")
    assert resolved.version == 4
    assert resolved.content == "v4 content"
    assert resolved.advisory is None
