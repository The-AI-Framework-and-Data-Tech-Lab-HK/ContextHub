from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from contexthub.api.routers.admin import router as admin_router
from contexthub.models.feedback import QualityReport
from contexthub.models.lifecycle import LifecyclePolicy
from contexthub.models.request import RequestContext


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


class AdminDB:
    def __init__(
        self,
        *,
        is_admin: bool = True,
        policies: list[dict] | None = None,
        transition_row: dict | None = None,
    ):
        self.is_admin = is_admin
        self.policies = policies or []
        self.transition_row = transition_row

    async def fetchval(self, sql, *args):
        if "FROM team_memberships" in sql:
            return 1 if self.is_admin else None
        return None

    async def fetch(self, sql, *args):
        if "FROM lifecycle_policies" in sql:
            return self.policies
        return []

    async def fetchrow(self, sql, *args):
        if "FROM contexts" in sql:
            return self.transition_row
        return None

    async def execute(self, sql, *args):
        return "OK"


class StubFeedbackService:
    def __init__(self):
        self.calls = []

    async def generate_quality_report(self, db, min_active_count=10, max_adoption_rate=0.2, limit=50):
        self.calls.append(
            {
                "db": db,
                "min_active_count": min_active_count,
                "max_adoption_rate": max_adoption_rate,
                "limit": limit,
            }
        )
        return QualityReport(
            items=[],
            total=0,
            min_active_count=min_active_count,
            max_adoption_rate=max_adoption_rate,
        )


class StubLifecycleService:
    def __init__(self):
        self.ensure_calls = []
        self.upsert_calls = []
        self.transition_calls = []

    async def ensure_default_policies(self, db, ctx):
        self.ensure_calls.append((db, ctx))

    async def upsert_policy(
        self,
        db,
        context_type,
        scope,
        stale_after_days,
        archive_after_days,
        delete_after_days,
        ctx,
    ):
        self.upsert_calls.append(
            {
                "db": db,
                "context_type": context_type,
                "scope": scope,
                "stale_after_days": stale_after_days,
                "archive_after_days": archive_after_days,
                "delete_after_days": delete_after_days,
                "ctx": ctx,
            }
        )
        return LifecyclePolicy(
            context_type=str(getattr(context_type, "value", context_type)),
            scope=str(getattr(scope, "value", scope)),
            stale_after_days=stale_after_days,
            archive_after_days=archive_after_days,
            delete_after_days=delete_after_days,
            account_id=ctx.account_id,
        )

    async def mark_stale(self, db, context_id, reason, ctx):
        self.transition_calls.append(("mark_stale", context_id, reason, ctx.agent_id))

    async def recover_from_stale(self, db, context_id, ctx):
        self.transition_calls.append(("recover_from_stale", context_id, None, ctx.agent_id))

    async def mark_archived(self, db, context_id, ctx):
        self.transition_calls.append(("mark_archived", context_id, None, ctx.agent_id))

    async def recover_from_archived(self, db, context_id, ctx):
        self.transition_calls.append(("recover_from_archived", context_id, None, ctx.agent_id))

    async def mark_deleted(self, db, context_id, ctx):
        self.transition_calls.append(("mark_deleted", context_id, None, ctx.agent_id))


class StubScheduler:
    def __init__(self):
        self.calls = 0

    async def run_once(self):
        self.calls += 1


@pytest_asyncio.fixture
async def admin_http_client():
    async def factory(db, *, feedback_service=None, lifecycle_service=None, scheduler=None):
        app = FastAPI()
        app.include_router(admin_router)
        app.state.repo = FakeRepo(db)
        app.state.feedback_service = feedback_service or StubFeedbackService()
        app.state.lifecycle_service = lifecycle_service or StubLifecycleService()
        app.state.lifecycle_scheduler = scheduler
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        return client

    yield factory


@pytest.mark.asyncio
async def test_quality_report_requires_admin(admin_http_client):
    client = await admin_http_client(AdminDB(is_admin=False))
    try:
        response = await client.get(
            "/api/v1/admin/quality-report",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_quality_report_passes_query_params_to_service(admin_http_client):
    feedback = StubFeedbackService()
    client = await admin_http_client(AdminDB(), feedback_service=feedback)
    try:
        response = await client.get(
            "/api/v1/admin/quality-report",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "admin-agent"},
            params={"min_active_count": 12, "max_adoption_rate": 0.4, "limit": 5},
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert feedback.calls == [
        {
            "db": feedback.calls[0]["db"],
            "min_active_count": 12,
            "max_adoption_rate": 0.4,
            "limit": 5,
        }
    ]


@pytest.mark.asyncio
async def test_lifecycle_policies_get_seeds_defaults_before_query(admin_http_client):
    lifecycle = StubLifecycleService()
    db = AdminDB(
        policies=[
            {
                "context_type": "memory",
                "scope": "team",
                "stale_after_days": 1,
                "archive_after_days": 2,
                "delete_after_days": 3,
                "account_id": "acme",
                "updated_at": None,
            }
        ]
    )
    client = await admin_http_client(db, lifecycle_service=lifecycle)
    try:
        response = await client.get(
            "/api/v1/admin/lifecycle/policies",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "admin-agent"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert lifecycle.ensure_calls
    assert response.json()[0]["context_type"] == "memory"


@pytest.mark.asyncio
async def test_lifecycle_policy_put_calls_upsert(admin_http_client):
    lifecycle = StubLifecycleService()
    client = await admin_http_client(AdminDB(), lifecycle_service=lifecycle)
    try:
        response = await client.put(
            "/api/v1/admin/lifecycle/policies",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "admin-agent"},
            json={
                "context_type": "resource",
                "scope": "team",
                "stale_after_days": 4,
                "archive_after_days": 5,
                "delete_after_days": 6,
            },
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert lifecycle.upsert_calls
    assert lifecycle.upsert_calls[0]["stale_after_days"] == 4
    assert response.json()["delete_after_days"] == 6


@pytest.mark.asyncio
async def test_lifecycle_transition_rejects_invalid_state_graph(admin_http_client):
    lifecycle = StubLifecycleService()
    context_id = uuid.uuid4()
    client = await admin_http_client(
        AdminDB(transition_row={"id": context_id, "status": "active"}),
        lifecycle_service=lifecycle,
    )
    try:
        response = await client.post(
            "/api/v1/admin/lifecycle/transition",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "admin-agent"},
            json={"context_uri": "ctx://team/engineering/doc", "target_status": "archived"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 400
    assert not lifecycle.transition_calls


@pytest.mark.asyncio
async def test_lifecycle_transition_uses_context_uri_and_calls_service(admin_http_client):
    lifecycle = StubLifecycleService()
    context_id = uuid.uuid4()
    client = await admin_http_client(
        AdminDB(transition_row={"id": context_id, "status": "active"}),
        lifecycle_service=lifecycle,
    )
    try:
        response = await client.post(
            "/api/v1/admin/lifecycle/transition",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "admin-agent"},
            json={
                "context_uri": "ctx://team/engineering/doc",
                "target_status": "stale",
                "reason": "manual-check",
            },
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert lifecycle.transition_calls == [("mark_stale", context_id, "manual-check", "admin-agent")]
    assert response.json()["context_uri"] == "ctx://team/engineering/doc"


@pytest.mark.asyncio
async def test_lifecycle_sweep_calls_scheduler(admin_http_client):
    scheduler = StubScheduler()
    client = await admin_http_client(AdminDB(), scheduler=scheduler)
    try:
        response = await client.post(
            "/api/v1/admin/lifecycle/sweep",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "admin-agent"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert scheduler.calls == 1


@pytest.mark.asyncio
async def test_lifecycle_sweep_fails_when_scheduler_missing(admin_http_client):
    client = await admin_http_client(AdminDB(), scheduler=None)
    try:
        response = await client.post(
            "/api/v1/admin/lifecycle/sweep",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "admin-agent"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 503
