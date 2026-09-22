"""DB-backed check that a withheld node comes back with its reason attached.

Complements the in-memory tests in tests/test_retrieval.py: this drives the real
propagation path (an upstream change → mark_stale → dependency closure) against
PostgreSQL, then asserts retrieval explains the hole instead of leaving one.
Gated by CONTEXTHUB_INTEGRATION=1.
"""

import uuid

import pytest

from contexthub.models.context import ContextLevel, ContextType, Scope
from contexthub.models.request import RequestContext
from contexthub.models.search import SearchRequest


async def _insert_memory(db, text: str, slug: str) -> uuid.UUID:
    node_id = uuid.uuid4()
    await db.execute(
        """
        INSERT INTO contexts (id, uri, context_type, scope, owner_space, account_id,
                              l0_content, l1_content, l2_content)
        VALUES ($1, $2, 'memory', 'agent', 'query-agent', 'acme', $3, $4, $5)
        """,
        node_id,
        f"ctx://agent/query-agent/memories/{slug}-{uuid.uuid4().hex[:6]}",
        text[:80],
        text[:300],
        text,
    )
    return node_id


@pytest.mark.asyncio
async def test_stale_node_is_withheld_but_its_reason_is_reported(
    acme_session, services
):
    """One hop: the served answer loses the node but gains the explanation."""
    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    root_id = await _insert_memory(
        acme_session, "The user has lactose intolerance.", "root-health_condition"
    )
    derived_id = await _insert_memory(
        acme_session, "The user takes Quelmithin for it.", "cur-medication"
    )
    await acme_session.execute(
        "INSERT INTO dependencies (dependent_id, dependency_id, dep_type)"
        " VALUES ($1, $2, 'derived_from')",
        derived_id,
        root_id,
    )

    reason = "health_condition: lactose intolerance -> high blood pressure"
    await services.lifecycle.mark_stale(acme_session, derived_id, reason, ctx)

    request = SearchRequest(
        query="Quelmithin medication",
        top_k=10,
        level=ContextLevel.L2,
        include_stale=False,
        include_stale_notices=True,
        context_type=[ContextType.MEMORY],
        scope=[Scope.AGENT],
    )
    response = await services.retrieval.search(acme_session, request, ctx)

    # Still withheld from the served results.
    assert derived_id not in [
        row["id"]
        for row in await acme_session.fetch(
            "SELECT id FROM contexts WHERE uri = ANY($1::text[])",
            [result.uri for result in response.results],
        )
    ]
    # But no longer a silent hole.
    notices = {notice.uri: notice for notice in response.stale_notices}
    derived_uri = await acme_session.fetchval(
        "SELECT uri FROM contexts WHERE id = $1", derived_id
    )
    assert derived_uri in notices
    notice = notices[derived_uri]
    assert notice.reason == reason
    assert notice.stale_content == "The user takes Quelmithin for it."
    assert notice.validity_status == "stale"


@pytest.mark.asyncio
async def test_two_hop_closure_notice_names_the_nearest_upstream(
    acme_session, services
):
    """The gold form points at the immediate predecessor, not the root."""
    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    root_id = await _insert_memory(
        acme_session, "The user works for Initech.", "root-employer"
    )
    mid_id = await _insert_memory(
        acme_session, "The user works at the Portland office.", "cur-work_location"
    )
    leaf_id = await _insert_memory(
        acme_session, "The user commutes by tram to Rosewood Cafe.", "cur-commute"
    )
    for dependent, dependency in ((mid_id, root_id), (leaf_id, mid_id)):
        await acme_session.execute(
            "INSERT INTO dependencies (dependent_id, dependency_id, dep_type)"
            " VALUES ($1, $2, 'derived_from')",
            dependent,
            dependency,
        )

    # Marking the middle node stale invalidates the leaf through the closure.
    await services.lifecycle.mark_stale(
        acme_session, mid_id, "employer: Initech -> Globex", ctx
    )

    leaf_uri, mid_uri = await acme_session.fetchval(
        "SELECT uri FROM contexts WHERE id = $1", leaf_id
    ), await acme_session.fetchval("SELECT uri FROM contexts WHERE id = $1", mid_id)

    response = await services.retrieval.search(
        acme_session,
        SearchRequest(
            query="commutes tram Rosewood",
            top_k=10,
            level=ContextLevel.L2,
            include_stale=False,
            include_stale_notices=True,
            context_type=[ContextType.MEMORY],
            scope=[Scope.AGENT],
        ),
        ctx,
    )

    notices = {notice.uri: notice for notice in response.stale_notices}
    assert leaf_uri in notices
    leaf_notice = notices[leaf_uri]
    # The nearest upstream is the middle node, not the root.
    assert leaf_notice.source_uri == mid_uri
    assert leaf_notice.source_content == "The user works at the Portland office."
    # The generic closure wording is replaced by something that names a node.
    assert str(mid_id) not in (leaf_notice.reason or "")
    assert "dependency closure invalidated by" not in (leaf_notice.reason or "")


@pytest.mark.asyncio
async def test_notices_absent_unless_requested(acme_session, services):
    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    node_id = await _insert_memory(
        acme_session, "The user takes Quelmithin for it.", "cur-medication"
    )
    await services.lifecycle.mark_stale(acme_session, node_id, "upstream changed", ctx)

    response = await services.retrieval.search(
        acme_session,
        SearchRequest(
            query="Quelmithin medication",
            top_k=10,
            level=ContextLevel.L2,
            include_stale=False,
            context_type=[ContextType.MEMORY],
            scope=[Scope.AGENT],
        ),
        ctx,
    )
    assert response.results == []
    assert response.stale_notices == []


@pytest.mark.asyncio
async def test_notice_survives_a_full_fresh_candidate_set(acme_session, services):
    """A stale node must still be explained when fresh matches fill retrieve_k.

    Both strategies order fresh rows first and then LIMIT `top_k * factor`, so a
    stale row sits below every fresh match and is cut whenever the fresh matches
    alone fill the bound. The notice then vanishes -- not because nothing was
    withheld, but because the row never reached the filter. Every other test here
    uses a handful of nodes and a generous top_k, which is why none of them saw
    it: with top_k=8 the bound is 24, so 24+ fresh matches are enough.
    """
    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    root_id = await _insert_memory(
        acme_session, "The user has lactose intolerance.", "root-health_condition"
    )
    derived_id = await _insert_memory(
        acme_session,
        "The user regular appointment is a tutoring session weekly.",
        "cur-regular_appointment",
    )
    await acme_session.execute(
        "INSERT INTO dependencies (dependent_id, dependency_id, dep_type)"
        " VALUES ($1, $2, 'derived_from')",
        derived_id,
        root_id,
    )
    # Enough fresh rows matching the same query words to fill top_k * factor.
    for index in range(30):
        await _insert_memory(
            acme_session,
            f"The user regular note number {index} is unrelated.",
            f"filler-{index}",
        )
    await services.lifecycle.mark_stale(
        acme_session, derived_id, "health_condition changed", ctx
    )

    request = SearchRequest(
        query="The user regular appointment",
        top_k=8,
        level=ContextLevel.L2,
        include_stale=False,
        include_stale_notices=True,
        context_type=[ContextType.MEMORY],
        scope=[Scope.AGENT],
    )
    response = await services.retrieval.search(acme_session, request, ctx)

    derived_uri = await acme_session.fetchval(
        "SELECT uri FROM contexts WHERE id = $1", derived_id
    )
    assert derived_uri in {notice.uri for notice in response.stale_notices}
    # The notice pass explains; it never serves. The stale row must stay out of
    # the results even though a second query went and fetched it.
    assert derived_uri not in {result.uri for result in response.results}
    assert len(response.results) == 8


@pytest.mark.asyncio
async def test_notice_pass_does_not_displace_served_rows(acme_session, services):
    """Asking for notices must not cost a served slot.

    The notice pass queries the complement of the fresh set, so the servable rows
    are the same set with notices on and off. Order among equally-scored rows is
    not asserted: neither strategy has a deterministic tiebreak, so that order is
    unstable independently of this request flag.
    """
    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    stale_id = await _insert_memory(
        acme_session, "The user regular appointment is a tutoring session.", "cur-appt"
    )
    for index in range(30):
        await _insert_memory(
            acme_session,
            f"The user regular note {index} mentions appointment {index}.",
            f"filler-{index}",
        )
    await services.lifecycle.mark_stale(acme_session, stale_id, "upstream changed", ctx)

    def build(with_notices: bool) -> SearchRequest:
        return SearchRequest(
            query="The user regular appointment",
            top_k=8,
            level=ContextLevel.L2,
            include_stale=False,
            include_stale_notices=with_notices,
            context_type=[ContextType.MEMORY],
            scope=[Scope.AGENT],
        )

    without = await services.retrieval.search(acme_session, build(False), ctx)
    with_notices = await services.retrieval.search(acme_session, build(True), ctx)

    assert len(without.results) == len(with_notices.results) == 8
    assert without.stale_notices == []
    assert len(with_notices.stale_notices) == 1
    stale_uri = await acme_session.fetchval(
        "SELECT uri FROM contexts WHERE id = $1", stale_id
    )
    for response in (without, with_notices):
        assert stale_uri not in {result.uri for result in response.results}
