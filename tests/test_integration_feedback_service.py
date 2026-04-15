from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress

import pytest

from contexthub.errors import BadRequestError, ForbiddenError, NotFoundError
from contexthub.models.feedback import QualityReport
from contexthub.services.acl_service import ACLService
from contexthub.services.feedback_service import FeedbackService


async def _insert_context(
    db,
    uri: str,
    *,
    scope: str = "team",
    owner_space: str | None = "engineering/backend",
    context_type: str = "resource",
    status: str = "active",
    active_count: int = 0,
    adopted_count: int = 0,
    ignored_count: int = 0,
):
    row = await db.fetchrow(
        """
        INSERT INTO contexts (
            id, uri, context_type, scope, owner_space, account_id, status,
            l0_content, active_count, adopted_count, ignored_count
        )
        VALUES (
            $1, $2, $3, $4, $5, current_setting('app.account_id'), $6,
            'feedback fixture', $7, $8, $9
        )
        RETURNING id
        """,
        uuid.uuid4(),
        uri,
        context_type,
        scope,
        owner_space,
        status,
        active_count,
        adopted_count,
        ignored_count,
    )
    return row["id"]


def _service() -> FeedbackService:
    return FeedbackService(ACLService())


@pytest.mark.asyncio
async def test_record_feedback_is_idempotent_for_same_outcome(
    acme_session,
    query_agent_ctx,
):
    context_id = await _insert_context(
        acme_session,
        "ctx://team/engineering/backend/feedback/idempotent",
    )
    svc = _service()

    first = await svc.record_feedback(
        acme_session,
        "ctx://team/engineering/backend/feedback/idempotent",
        "rid-1",
        "adopted",
        query_agent_ctx,
        metadata={"source": "test"},
    )
    second = await svc.record_feedback(
        acme_session,
        "ctx://team/engineering/backend/feedback/idempotent",
        "rid-1",
        "adopted",
        query_agent_ctx,
        metadata={"source": "updated"},
    )

    counts = await acme_session.fetchrow(
        """
        SELECT adopted_count, ignored_count
        FROM contexts
        WHERE id = $1
        """,
        context_id,
    )
    feedback_rows = await acme_session.fetch(
        """
        SELECT id, metadata
        FROM context_feedback
        WHERE context_id = $1
        """,
        context_id,
    )

    assert first.id == second.id
    assert first.retrieval_id == "rid-1"
    assert len(feedback_rows) == 1
    assert feedback_rows[0]["metadata"] == {"source": "updated"}
    assert counts["adopted_count"] == 1
    assert counts["ignored_count"] == 0


@pytest.mark.asyncio
async def test_record_feedback_serializes_same_idempotency_key_across_transactions(
    repo,
    clean_db,
    query_agent_ctx,
):
    uri = "ctx://team/engineering/backend/feedback/concurrent-idempotent"
    async with repo.session("acme") as seed_db:
        context_id = await _insert_context(seed_db, uri)

    svc = _service()
    first_cm = repo.session("acme")
    second_cm = repo.session("acme")
    first_db = await first_cm.__aenter__()
    second_db = await second_cm.__aenter__()
    first_closed = False
    second_task = None

    try:
        first = await svc.record_feedback(
            first_db,
            uri,
            "rid-concurrent",
            "adopted",
            query_agent_ctx,
            metadata={"source": "first"},
        )

        second_task = asyncio.create_task(
            svc.record_feedback(
                second_db,
                uri,
                "rid-concurrent",
                "adopted",
                query_agent_ctx,
                metadata={"source": "second"},
            )
        )
        await asyncio.sleep(0.05)
        assert not second_task.done()

        await first_cm.__aexit__(None, None, None)
        first_closed = True
        second = await second_task
    finally:
        if second_task is not None and not second_task.done():
            second_task.cancel()
            with suppress(asyncio.CancelledError):
                await second_task
        if not first_closed:
            await first_cm.__aexit__(None, None, None)
        await second_cm.__aexit__(None, None, None)

    async with repo.session("acme") as verify_db:
        counts = await verify_db.fetchrow(
            """
            SELECT adopted_count, ignored_count
            FROM contexts
            WHERE id = $1
            """,
            context_id,
        )
        feedback_rows = await verify_db.fetch(
            """
            SELECT id, metadata
            FROM context_feedback
            WHERE context_id = $1
            """,
            context_id,
        )

    assert first.id == second.id
    assert len(feedback_rows) == 1
    assert feedback_rows[0]["metadata"] == {"source": "second"}
    assert counts["adopted_count"] == 1
    assert counts["ignored_count"] == 0


@pytest.mark.asyncio
async def test_record_feedback_updates_counts_when_outcome_crosses_buckets(
    acme_session,
    query_agent_ctx,
):
    context_id = await _insert_context(
        acme_session,
        "ctx://team/engineering/backend/feedback/cross-bucket",
    )
    svc = _service()

    await svc.record_feedback(
        acme_session,
        "ctx://team/engineering/backend/feedback/cross-bucket",
        "rid-2",
        "adopted",
        query_agent_ctx,
    )
    updated = await svc.record_feedback(
        acme_session,
        "ctx://team/engineering/backend/feedback/cross-bucket",
        "rid-2",
        "ignored",
        query_agent_ctx,
    )

    counts = await acme_session.fetchrow(
        """
        SELECT adopted_count, ignored_count
        FROM contexts
        WHERE id = $1
        """,
        context_id,
    )
    stored = await acme_session.fetchrow(
        """
        SELECT outcome
        FROM context_feedback
        WHERE context_id = $1 AND retrieval_id = 'rid-2'
        """,
        context_id,
    )

    assert updated.outcome.value == "ignored"
    assert stored["outcome"] == "ignored"
    assert counts["adopted_count"] == 0
    assert counts["ignored_count"] == 1


@pytest.mark.asyncio
async def test_record_feedback_same_bucket_update_is_noop_for_counts(
    acme_session,
    query_agent_ctx,
):
    context_id = await _insert_context(
        acme_session,
        "ctx://team/engineering/backend/feedback/same-bucket",
    )
    svc = _service()

    await svc.record_feedback(
        acme_session,
        "ctx://team/engineering/backend/feedback/same-bucket",
        "rid-3",
        "adopted",
        query_agent_ctx,
    )
    updated = await svc.record_feedback(
        acme_session,
        "ctx://team/engineering/backend/feedback/same-bucket",
        "rid-3",
        "corrected",
        query_agent_ctx,
    )

    counts = await acme_session.fetchrow(
        """
        SELECT adopted_count, ignored_count
        FROM contexts
        WHERE id = $1
        """,
        context_id,
    )

    assert updated.outcome.value == "corrected"
    assert counts["adopted_count"] == 1
    assert counts["ignored_count"] == 0


@pytest.mark.asyncio
async def test_record_feedback_same_bucket_update_is_noop_for_counts_ignored_to_irrelevant(
    acme_session,
    query_agent_ctx,
):
    context_id = await _insert_context(
        acme_session,
        "ctx://team/engineering/backend/feedback/same-bucket-ignored",
    )
    svc = _service()

    await svc.record_feedback(
        acme_session,
        "ctx://team/engineering/backend/feedback/same-bucket-ignored",
        "rid-3b",
        "ignored",
        query_agent_ctx,
    )
    updated = await svc.record_feedback(
        acme_session,
        "ctx://team/engineering/backend/feedback/same-bucket-ignored",
        "rid-3b",
        "irrelevant",
        query_agent_ctx,
    )

    counts = await acme_session.fetchrow(
        """
        SELECT adopted_count, ignored_count
        FROM contexts
        WHERE id = $1
        """,
        context_id,
    )

    assert updated.outcome.value == "irrelevant"
    assert counts["adopted_count"] == 0
    assert counts["ignored_count"] == 1


@pytest.mark.asyncio
async def test_record_feedback_generates_retrieval_id_when_missing(
    acme_session,
    query_agent_ctx,
):
    await _insert_context(
        acme_session,
        "ctx://team/engineering/backend/feedback/degraded",
    )
    svc = _service()

    recorded = await svc.record_feedback(
        acme_session,
        "ctx://team/engineering/backend/feedback/degraded",
        "",
        "ignored",
        query_agent_ctx,
    )

    assert uuid.UUID(recorded.retrieval_id)
    assert recorded.outcome.value == "ignored"


@pytest.mark.asyncio
async def test_record_feedback_rejects_missing_context_invalid_outcome_and_forbidden(
    acme_session,
    query_agent_ctx,
):
    svc = _service()

    with pytest.raises(NotFoundError, match="ctx://team/engineering/backend/feedback/missing"):
        await svc.record_feedback(
            acme_session,
            "ctx://team/engineering/backend/feedback/missing",
            "rid-missing",
            "adopted",
            query_agent_ctx,
        )

    await _insert_context(
        acme_session,
        "ctx://team/data/analytics/feedback/hidden",
        owner_space="data/analytics",
    )
    with pytest.raises(ForbiddenError):
        await svc.record_feedback(
            acme_session,
            "ctx://team/data/analytics/feedback/hidden",
            "rid-hidden",
            "adopted",
            query_agent_ctx,
        )

    await _insert_context(
        acme_session,
        "ctx://team/engineering/backend/feedback/bad-outcome",
    )
    with pytest.raises(BadRequestError, match="Invalid feedback outcome"):
        await svc.record_feedback(
            acme_session,
            "ctx://team/engineering/backend/feedback/bad-outcome",
            "rid-bad",
            "maybe",
            query_agent_ctx,
        )


@pytest.mark.asyncio
async def test_record_feedback_returns_not_found_when_context_is_deleted_concurrently(
    repo,
    clean_db,
    query_agent_ctx,
):
    uri = "ctx://team/engineering/backend/feedback/deleted-concurrently"
    async with repo.session("acme") as seed_db:
        context_id = await _insert_context(seed_db, uri)

    svc = _service()
    delete_cm = repo.session("acme")
    feedback_cm = repo.session("acme")
    delete_db = await delete_cm.__aenter__()
    feedback_db = await feedback_cm.__aenter__()
    delete_closed = False
    feedback_task = None

    try:
        await delete_db.execute(
            """
            UPDATE contexts
            SET status = 'deleted',
                deleted_at = NOW(),
                version = version + 1,
                updated_at = NOW()
            WHERE uri = $1
            """,
            uri,
        )

        feedback_task = asyncio.create_task(
            svc.record_feedback(
                feedback_db,
                uri,
                "rid-deleted",
                "adopted",
                query_agent_ctx,
            )
        )
        await asyncio.sleep(0.05)
        assert not feedback_task.done()

        await delete_cm.__aexit__(None, None, None)
        delete_closed = True

        with pytest.raises(NotFoundError, match=uri):
            await feedback_task
    finally:
        if feedback_task is not None and not feedback_task.done():
            feedback_task.cancel()
            with suppress(asyncio.CancelledError):
                await feedback_task
        if not delete_closed:
            await delete_cm.__aexit__(None, None, None)
        await feedback_cm.__aexit__(None, None, None)

    async with repo.session("acme") as verify_db:
        context_row = await verify_db.fetchrow(
            """
            SELECT status, adopted_count, ignored_count
            FROM contexts
            WHERE id = $1
            """,
            context_id,
        )
        feedback_count = await verify_db.fetchval(
            """
            SELECT COUNT(*)
            FROM context_feedback
            WHERE context_id = $1
            """,
            context_id,
        )

    assert context_row["status"] == "deleted"
    assert context_row["adopted_count"] == 0
    assert context_row["ignored_count"] == 0
    assert feedback_count == 0


@pytest.mark.asyncio
async def test_get_quality_score_and_report_exclude_low_sample_contexts(acme_session):
    reportable_id = await _insert_context(
        acme_session,
        "ctx://team/engineering/backend/feedback/reportable",
        active_count=25,
        adopted_count=1,
        ignored_count=5,
    )
    await _insert_context(
        acme_session,
        "ctx://team/engineering/backend/feedback/low-sample",
        active_count=200,
        adopted_count=0,
        ignored_count=0,
    )
    await _insert_context(
        acme_session,
        "ctx://team/engineering/backend/feedback/good",
        active_count=50,
        adopted_count=4,
        ignored_count=1,
    )
    svc = _service()

    score = await svc.get_quality_score(acme_session, reportable_id)
    report = await svc.generate_quality_report(
        acme_session,
        min_active_count=10,
        max_adoption_rate=0.3,
        limit=10,
    )

    assert score == pytest.approx(1 / 7)
    assert isinstance(report, QualityReport)
    assert report.total == 1
    assert report.items[0].uri == "ctx://team/engineering/backend/feedback/reportable"
    assert report.items[0].adoption_rate == pytest.approx(1 / 6)
    assert report.items[0].quality_score == pytest.approx(1 / 7)


@pytest.mark.asyncio
async def test_list_feedback_supports_filters_and_stable_order(
    acme_session,
    query_agent_ctx,
):
    first_context_id = await _insert_context(
        acme_session,
        "ctx://team/engineering/backend/feedback/list-one",
    )
    second_context_id = await _insert_context(
        acme_session,
        "ctx://team/engineering/backend/feedback/list-two",
    )
    svc = _service()

    await svc.record_feedback(
        acme_session,
        "ctx://team/engineering/backend/feedback/list-one",
        "rid-shared",
        "adopted",
        query_agent_ctx,
    )
    await svc.record_feedback(
        acme_session,
        "ctx://team/engineering/backend/feedback/list-two",
        "rid-shared",
        "ignored",
        query_agent_ctx,
    )
    await svc.record_feedback(
        acme_session,
        "ctx://team/engineering/backend/feedback/list-one",
        "rid-other",
        "corrected",
        query_agent_ctx,
    )

    all_rows = await svc.list_feedback(acme_session)
    by_context = await svc.list_feedback(acme_session, context_id=first_context_id)
    by_retrieval = await svc.list_feedback(acme_session, retrieval_id="rid-shared")
    combined = await svc.list_feedback(
        acme_session,
        context_id=first_context_id,
        retrieval_id="rid-shared",
    )

    assert [row.retrieval_id for row in all_rows] == ["rid-other", "rid-shared", "rid-shared"]
    assert {row.context_id for row in by_context} == {first_context_id}
    assert {row.context_id for row in by_retrieval} == {first_context_id, second_context_id}
    assert len(combined) == 1
    assert combined[0].context_id == first_context_id
    assert combined[0].retrieval_id == "rid-shared"


@pytest.mark.asyncio
async def test_get_quality_score_raises_for_missing_context(acme_session):
    svc = _service()

    with pytest.raises(NotFoundError):
        await svc.get_quality_score(acme_session, uuid.uuid4())
