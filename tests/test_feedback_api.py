from __future__ import annotations

from datetime import datetime, timezone
import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from contexthub.api.routers.feedback import router as feedback_router
from contexthub.models.feedback import ContextFeedback
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService
from contexthub.services.feedback_service import FeedbackService


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


async def _insert_context(
    db,
    uri: str,
    *,
    scope: str = "team",
    owner_space: str | None = "engineering/backend",
    context_type: str = "resource",
    status: str = "active",
):
    return await db.fetchval(
        """
        INSERT INTO contexts (
            id, uri, context_type, scope, owner_space, account_id, status, l0_content
        )
        VALUES (
            $1, $2, $3, $4, $5, current_setting('app.account_id'), $6, 'feedback fixture'
        )
        RETURNING id
        """,
        uuid.uuid4(),
        uri,
        context_type,
        scope,
        owner_space,
        status,
    )


async def _insert_deny_policy(db, pattern: str, principal: str):
    await db.execute(
        """
        INSERT INTO access_policies (
            resource_uri_pattern, principal, effect, actions,
            conditions, field_masks, priority, account_id, created_by
        )
        VALUES (
            $1, $2, 'deny', ARRAY['read']::text[],
            NULL, NULL, 0, current_setting('app.account_id'), 'test'
        )
        """,
        pattern,
        principal,
    )


@pytest_asyncio.fixture
async def feedback_http_client():
    def build(feedback_service, repo, acl_service=None):
        app = FastAPI()
        app.include_router(feedback_router)
        app.state.repo = repo
        app.state.feedback_service = feedback_service
        app.state.acl_service = acl_service or ACLService()
        return app

    async def factory(feedback_service, *, repo, acl_service=None):
        app = build(feedback_service, repo, acl_service=acl_service)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        return client

    yield factory


@pytest.mark.asyncio
async def test_feedback_router_maps_request_and_headers(feedback_http_client):
    class StubFeedbackService:
        def __init__(self):
            self.calls = []

        async def record_feedback(
            self,
            db,
            context_uri: str,
            retrieval_id: str | None,
            outcome: str,
            ctx: RequestContext,
            metadata: dict | None = None,
        ) -> ContextFeedback:
            self.calls.append(
                {
                    "db": db,
                    "context_uri": context_uri,
                    "retrieval_id": retrieval_id,
                    "outcome": outcome,
                    "ctx": ctx,
                    "metadata": metadata,
                }
            )
            return ContextFeedback(
                id=1,
                context_id=uuid.uuid4(),
                retrieval_id=retrieval_id or "rid-generated",
                actor=ctx.agent_id,
                retrieved_at=datetime.now(timezone.utc),
                outcome=outcome,
                metadata=metadata,
                account_id=ctx.account_id,
                created_at=datetime.now(timezone.utc),
            )

    service = StubFeedbackService()
    sentinel_db = object()
    client = await feedback_http_client(service, repo=FakeRepo(sentinel_db))
    try:
        response = await client.post(
            "/api/v1/feedback",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"},
            json={
                "context_uri": "ctx://team/engineering/backend/feedback/router",
                "outcome": "adopted",
                "retrieval_id": "rid-123",
                "metadata": {"source": "explicit-search"},
            },
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert service.calls
    call = service.calls[0]
    assert call["context_uri"] == "ctx://team/engineering/backend/feedback/router"
    assert call["retrieval_id"] == "rid-123"
    assert call["outcome"] == "adopted"
    assert call["metadata"] == {"source": "explicit-search"}
    assert call["db"] is sentinel_db
    assert call["ctx"].account_id == "acme"
    assert call["ctx"].agent_id == "query-agent"


@pytest.mark.asyncio
async def test_feedback_http_route_records_feedback_end_to_end(
    feedback_http_client,
    repo,
    clean_db,
):
    uri = "ctx://team/engineering/backend/feedback/http"
    async with repo.session("acme") as db:
        await _insert_context(db, uri)

    client = await feedback_http_client(FeedbackService(ACLService()), repo=repo)
    try:
        response = await client.post(
            "/api/v1/feedback",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"},
            json={
                "context_uri": uri,
                "outcome": "ignored",
            },
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == "acme"
    assert body["actor"] == "query-agent"
    assert body["outcome"] == "ignored"
    assert body["retrieval_id"]
    assert uuid.UUID(body["retrieval_id"])

    async with repo.session("acme") as db:
        row = await db.fetchrow(
            """
            SELECT retrieval_id, outcome, actor, account_id
            FROM context_feedback
            WHERE context_id = $1
            """,
            uuid.UUID(body["context_id"]),
        )
    assert row is not None
    assert row["actor"] == "query-agent"
    assert row["account_id"] == "acme"
    assert row["outcome"] == "ignored"


@pytest.mark.asyncio
async def test_feedback_list_requires_at_least_one_filter(feedback_http_client):
    client = await feedback_http_client(FeedbackService(ACLService()), repo=FakeRepo(object()))
    try:
        response = await client.get(
            "/api/v1/feedback",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 400
    assert "context_id or retrieval_id" in response.json()["detail"]


@pytest.mark.asyncio
async def test_feedback_list_filters_by_context_id_and_retrieval_id(
    feedback_http_client,
    repo,
    clean_db,
):
    visible_uri = "ctx://team/engineering/backend/feedback/list-visible"
    hidden_uri = "ctx://datalake/prod/feedback/list-hidden"

    async with repo.session("acme") as db:
        visible_id = await _insert_context(db, visible_uri)
        hidden_id = await _insert_context(db, hidden_uri, scope="datalake", owner_space=None)

        service = FeedbackService(ACLService())
        ctx = RequestContext(account_id="acme", agent_id="query-agent")
        await service.record_feedback(db, visible_uri, "rid-shared", "adopted", ctx)
        await service.record_feedback(db, hidden_uri, "rid-shared", "ignored", ctx)
        await _insert_deny_policy(db, hidden_uri, "query-agent")

    client = await feedback_http_client(FeedbackService(ACLService()), repo=repo)
    try:
        by_context = await client.get(
            "/api/v1/feedback",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"},
            params={"context_id": str(visible_id)},
        )
        by_retrieval = await client.get(
            "/api/v1/feedback",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"},
            params={"retrieval_id": "rid-shared"},
        )
        hidden_context = await client.get(
            "/api/v1/feedback",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"},
            params={"context_id": str(hidden_id)},
        )
    finally:
        await client.aclose()

    assert by_context.status_code == 200
    context_items = by_context.json()
    assert len(context_items) == 1
    assert context_items[0]["context_id"] == str(visible_id)

    assert by_retrieval.status_code == 200
    retrieval_items = by_retrieval.json()
    assert len(retrieval_items) == 1
    assert retrieval_items[0]["context_id"] == str(visible_id)
    assert retrieval_items[0]["retrieval_id"] == "rid-shared"

    assert hidden_context.status_code == 403
