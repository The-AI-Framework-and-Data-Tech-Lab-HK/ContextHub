"""Tier 3 DB-backed test for multi-hop derived_from cascade (M1).

Verifies the marked_stale cascade gate: with cascade_on_stale=True and the real
oracle rule wired in, an upstream modified event propagates staleness along a
2-hop derived_from chain root → A → B, reaching B.

Setup and assertions use committed repo.session() blocks (not the open
acme_session transaction) so the engine's independent connections observe the
graph — this mirrors how the real eval harness drives the engine.

Gated by CONTEXTHUB_INTEGRATION=1.
"""

import uuid

import pytest

from contexthub.propagation.registry import PropagationRuleRegistry
from contexthub.services.propagation_engine import PropagationEngine


class _YesOracleChat:
    """Stub chat client: always judges the derived note stale (YES).

    Isolates the cascade mechanism from LLM quality — this test asserts the
    engine propagates through marked_stale, not that a model judges correctly.
    """

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        return "YES\nupstream changed, derived note is now outdated"


async def _insert_memory(db, slug: str, l2: str) -> uuid.UUID:
    mem_id = uuid.uuid4()
    await db.execute(
        """
        INSERT INTO contexts (id, uri, context_type, scope, owner_space, account_id,
                              l0_content, l1_content, l2_content)
        VALUES ($1, $2, 'memory', 'agent', 'query-agent', 'acme', $3, $3, $4)
        """,
        mem_id,
        f"ctx://agent/query-agent/memories/{slug}-{uuid.uuid4().hex[:6]}",
        l2[:80],
        l2,
    )
    return mem_id


async def _make_engine(repo, db_pool, services, cascade_on_stale: bool):
    registry = PropagationRuleRegistry.default(chat_client=_YesOracleChat(), repo=repo)
    engine = PropagationEngine(
        repo=repo,
        pool=db_pool,
        dsn="postgresql://contexthub:contexthub@localhost:5432/contexthub",
        rule_registry=registry,
        lifecycle=services.lifecycle,
        indexer=services.indexer,
        sweep_interval=9999,
        lease_timeout=5,
        cascade_on_stale=cascade_on_stale,
    )
    engine._running = True
    return engine


async def _build_chain(repo):
    """root --derived_from--> A --derived_from--> B, + a root modified event.

    Returns (root_id, a_id, b_id). Committed via repo.session so the engine sees it.
    """
    async with repo.session("acme") as db:
        root_id = await _insert_memory(db, "root", "team lead is Seokjin Kang")
        a_id = await _insert_memory(db, "hop1", "weekly report recipient is Hyunwoo Nam")
        b_id = await _insert_memory(db, "hop2", "report distribution list derived from recipient")
        await db.execute(
            "INSERT INTO dependencies (dependent_id, dependency_id, dep_type) VALUES ($1, $2, 'derived_from')",
            a_id, root_id,
        )
        await db.execute(
            "INSERT INTO dependencies (dependent_id, dependency_id, dep_type) VALUES ($1, $2, 'derived_from')",
            b_id, a_id,
        )
        await db.execute(
            "UPDATE contexts SET l2_content = 'team lead is Jihoon Ryu', updated_at = NOW() WHERE id = $1",
            root_id,
        )
        await db.execute(
            """
            INSERT INTO change_events (context_id, account_id, change_type, actor, diff_summary)
            VALUES ($1, 'acme', 'modified', 'meme_eval', 'team_lead: Seokjin Kang -> Jihoon Ryu')
            """,
            root_id,
        )
    return root_id, a_id, b_id


async def _status(repo, ctx_id) -> str:
    async with repo.session("acme") as db:
        row = await db.fetchrow("SELECT status FROM contexts WHERE id = $1", ctx_id)
    return row["status"]


@pytest.mark.asyncio
async def test_two_hop_cascade_reaches_leaf(db_pool, repo, clean_db, services):
    """Modifying root should stale A (hop-1) and B (hop-2) with the gate ON."""
    root_id, a_id, b_id = await _build_chain(repo)

    engine = await _make_engine(repo, db_pool, services, cascade_on_stale=True)
    for _ in range(5):
        await engine._drain_ready_events(context_id=None)

    assert await _status(repo, a_id) == "stale", "hop-1 A should be stale"
    assert await _status(repo, b_id) == "stale", "hop-2 B should be stale (cascade)"


@pytest.mark.asyncio
async def test_cascade_gate_off_stops_at_hop1(db_pool, repo, clean_db, services):
    """With the gate OFF (default), staleness must NOT propagate past hop-1."""
    root_id, a_id, b_id = await _build_chain(repo)

    engine = await _make_engine(repo, db_pool, services, cascade_on_stale=False)
    for _ in range(5):
        await engine._drain_ready_events(context_id=None)

    assert await _status(repo, a_id) == "stale", "hop-1 stales from the direct modified event"
    assert await _status(repo, b_id) == "active", "hop-2 must NOT stale with gate off"
