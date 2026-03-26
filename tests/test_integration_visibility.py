"""Tier 3 visibility and isolation tests (A-1 ~ A-4).

Gated by CONTEXTHUB_INTEGRATION=1.
"""

import uuid

import pytest
import pytest_asyncio

from contexthub.models.memory import AddMemoryRequest, PromoteRequest


@pytest.mark.asyncio
async def test_a1_agent_private_isolation(acme_session, services, query_agent_ctx, analysis_agent_ctx):
    """A-1: Agent private memory not visible to other agents."""
    body = AddMemoryRequest(content="Secret query optimization trick", tags=["private"])
    mem = await services.memory.add_memory(acme_session, body, query_agent_ctx)

    # analysis-agent should NOT see query-agent's private memory
    memories = await services.memory.list_memories(acme_session, analysis_agent_ctx)
    uris = [m["uri"] for m in memories]
    assert mem.uri not in uris


@pytest.mark.asyncio
async def test_a2_team_hierarchy_child_reads_parent(acme_session, services, query_agent_ctx):
    """A-2: Content in parent team visible to child team member."""
    # Write content at team/engineering level
    ctx_id = uuid.uuid4()
    await acme_session.execute(
        """
        INSERT INTO contexts (id, uri, context_type, scope, owner_space, account_id,
                              l0_content, l1_content)
        VALUES ($1, 'ctx://team/engineering/docs/coding-standards', 'memory', 'team', 'engineering', 'acme',
                'Coding standards', 'Use type hints everywhere')
        """,
        ctx_id,
    )

    # query-agent is in engineering/backend (child) — should see parent content
    visible = await services.acl.check_read(
        acme_session, "ctx://team/engineering/docs/coding-standards", query_agent_ctx,
    )
    assert visible is True


@pytest.mark.asyncio
async def test_a3_child_team_not_visible_to_parent(acme_session, services, analysis_agent_ctx):
    """A-3: Content in child team NOT visible to parent team member (by default)."""
    ctx_id = uuid.uuid4()
    await acme_session.execute(
        """
        INSERT INTO contexts (id, uri, context_type, scope, owner_space, account_id,
                              l0_content, l1_content)
        VALUES ($1, 'ctx://team/engineering/backend/internal-notes', 'memory', 'team', 'engineering/backend', 'acme',
                'Backend internal', 'Internal notes')
        """,
        ctx_id,
    )

    # analysis-agent is in engineering (parent) but NOT in engineering/backend
    visible = await services.acl.check_read(
        acme_session, "ctx://team/engineering/backend/internal-notes", analysis_agent_ctx,
    )
    assert visible is False


@pytest.mark.asyncio
async def test_a4_promote_makes_visible(acme_session, services, query_agent_ctx, analysis_agent_ctx):
    """A-4: After promote, content visible to other team members."""
    body = AddMemoryRequest(content="Useful pattern for batch processing", tags=["pattern"])
    mem = await services.memory.add_memory(acme_session, body, query_agent_ctx)

    # Before promote: not visible to analysis-agent
    memories_before = await services.memory.list_memories(acme_session, analysis_agent_ctx)
    assert mem.uri not in [m["uri"] for m in memories_before]

    # Promote
    promote_body = PromoteRequest(uri=mem.uri, target_team="engineering")
    promoted = await services.memory.promote(acme_session, promote_body, query_agent_ctx)

    # After promote: visible
    memories_after = await services.memory.list_memories(acme_session, analysis_agent_ctx)
    promoted_uris = [m["uri"] for m in memories_after if "shared_knowledge" in m["uri"]]
    assert len(promoted_uris) >= 1
