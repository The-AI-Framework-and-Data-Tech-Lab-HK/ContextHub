"""Phase 2 integration tests: A-5 ~ A-15, Admin API HTTP smoke tests, SDK structure tests."""

from __future__ import annotations

import json
from uuid import UUID

import pytest
import pytest_asyncio


# ── Helper functions ────────────────────────────────────────────────────


async def _make_admin(db, agent_id, team_id="00000000-0000-0000-0000-000000000003"):
    """Promote agent to admin role in the given team."""
    await db.execute(
        """
        INSERT INTO team_memberships (agent_id, team_id, role, access)
        VALUES ($1, $2::uuid, 'admin', 'read_write')
        ON CONFLICT (agent_id, team_id)
        DO UPDATE SET role = 'admin'
        """,
        agent_id,
        team_id,
    )


async def _insert_context(db, uri, scope="datalake", owner_space=None,
                           l0="", l1="", l2="", context_type="resource"):
    """Insert a test context row. Returns id."""
    row = await db.fetchrow(
        """
        INSERT INTO contexts
            (uri, context_type, scope, owner_space, account_id,
             l0_content, l1_content, l2_content)
        VALUES ($1, $2, $3, $4, current_setting('app.account_id'),
                $5, $6, $7)
        RETURNING id
        """,
        uri, context_type, scope, owner_space, l0, l1, l2,
    )
    return row["id"]


async def _insert_policy(db, pattern, principal, effect, actions=None,
                          field_masks=None, priority=0, conditions=None):
    """Insert an access policy row. Returns id."""
    row = await db.fetchrow(
        """
        INSERT INTO access_policies
            (resource_uri_pattern, principal, effect, actions,
             field_masks, priority, account_id, conditions)
        VALUES ($1, $2, $3, $4::text[], $5, $6,
                current_setting('app.account_id'), $7::jsonb)
        RETURNING id
        """,
        pattern, principal, effect,
        actions or ["read"],
        field_masks,
        priority,
        json.dumps(conditions) if conditions else None,
    )
    return row["id"]


async def _count_audit(db, action=None, resource_uri=None, result=None):
    """Count matching audit_log rows."""
    conditions = []
    args = []
    idx = 1
    if action:
        conditions.append(f"action = ${idx}")
        args.append(action)
        idx += 1
    if resource_uri:
        conditions.append(f"resource_uri = ${idx}")
        args.append(resource_uri)
        idx += 1
    if result:
        conditions.append(f"result = ${idx}")
        args.append(result)
        idx += 1
    where = " AND ".join(conditions) if conditions else "TRUE"
    return await db.fetchval(f"SELECT COUNT(*) FROM audit_log WHERE {where}", *args)


# ── A-5 ~ A-15 Integration Tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_a5_deny_override(acme_session, phase2_services):
    """A-5: Deny policy overrides allow policy on the same resource.

    Seed data: query-agent is a direct member of both engineering and
    engineering/backend.  _check_hierarchy_deny only fires for ancestor-only
    paths (not direct memberships), so the deny on "engineering" is matched
    as a regular explicit deny — the ACL engine returns "explicit deny".
    The business-level assertion (deny wins over allow) still holds.
    """
    db = acme_session
    acl = phase2_services.acl
    from contexthub.models.request import RequestContext

    await _insert_context(db, "ctx://datalake/prod/salary/details",
                          scope="datalake", l1="Employee salary: 100000")

    await _insert_policy(db, "ctx://datalake/prod/salary/*", "engineering", "deny")
    await _insert_policy(db, "ctx://datalake/prod/salary/*", "engineering/backend", "allow")

    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    decision = await acl.check_read_access(db, "ctx://datalake/prod/salary/details", ctx)
    assert decision.allowed is False
    assert decision.reason == "explicit deny"


@pytest.mark.asyncio
async def test_a6_keyword_masking(acme_session, phase2_services):
    """A-6: field_masks keywords are replaced with [MASKED] in content."""
    db = acme_session
    acl = phase2_services.acl
    masking = phase2_services.masking
    from contexthub.models.request import RequestContext

    await _insert_context(db, "ctx://datalake/prod/employee/record",
                          scope="datalake",
                          l0="salary info: 100000",
                          l1="Employee salary: 100000 bonus: 5000",
                          l2="Full record: salary=100000 bonus=5000 ssn=123-45-6789")

    await _insert_policy(db, "ctx://datalake/prod/employee/*", "engineering/backend",
                         "allow", actions=["read"],
                         field_masks=["salary", "ssn"])

    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    decision = await acl.check_read_access(db, "ctx://datalake/prod/employee/record", ctx)
    assert decision.allowed is True
    assert decision.field_masks is not None
    assert "salary" in decision.field_masks

    masked_l1 = masking.apply_masks("Employee salary: 100000 bonus: 5000",
                                     decision.field_masks)
    assert "[MASKED]" in masked_l1
    assert "salary" not in masked_l1.replace("[MASKED]", "")


@pytest.mark.asyncio
async def test_a7_allow_single_read(acme_session, phase2_services):
    """A-7: Explicit allow on a default-invisible resource enables read."""
    db = acme_session
    acl = phase2_services.acl
    from contexthub.models.request import RequestContext

    await _insert_context(db, "ctx://team/data/analytics/configs/secret",
                          scope="team", owner_space="data/analytics",
                          l1="secret config")

    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    decision_before = await acl.check_read_access(
        db, "ctx://team/data/analytics/configs/secret", ctx)
    assert decision_before.allowed is False

    await _insert_policy(db, "ctx://team/data/analytics/configs/secret",
                         "query-agent", "allow", actions=["read"])

    decision_after = await acl.check_read_access(
        db, "ctx://team/data/analytics/configs/secret", ctx)
    assert decision_after.allowed is True
    assert decision_after.reason == "explicit allow"


@pytest.mark.asyncio
async def test_a7b_allow_search_discoverable(acme_session, phase2_services):
    """A-7b: Explicit allow makes a default-invisible resource discoverable via filter_visible_with_acl."""
    db = acme_session
    acl = phase2_services.acl
    from contexthub.models.request import RequestContext

    await _insert_context(db, "ctx://team/data/analytics/configs/searchable",
                          scope="team", owner_space="data/analytics",
                          l1="searchable config")

    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    await _insert_policy(db, "ctx://team/data/analytics/configs/searchable",
                         "query-agent", "allow", actions=["read"])

    row = await db.fetchrow(
        "SELECT * FROM contexts WHERE uri = $1",
        "ctx://team/data/analytics/configs/searchable",
    )
    visible = await acl.filter_visible_with_acl(db, [dict(row)], ctx)
    assert len(visible) == 1


@pytest.mark.asyncio
async def test_a8_priority_resolution(acme_session, phase2_services):
    """A-8: Two allow policies, different priority → higher priority wins.

    The ACL engine returns the first allow (sorted by priority DESC).
    The higher-priority allow (priority=10, no field_masks) should be
    selected over the lower-priority one (priority=1, with field_masks).
    """
    db = acme_session
    acl = phase2_services.acl
    from contexthub.models.request import RequestContext

    await _insert_context(db, "ctx://datalake/prod/priority-test",
                          scope="datalake", l1="priority test data")

    await _insert_policy(db, "ctx://datalake/prod/priority-test",
                         "engineering/backend", "allow", priority=1,
                         field_masks=["salary"])
    await _insert_policy(db, "ctx://datalake/prod/priority-test",
                         "engineering/backend", "allow", priority=10,
                         field_masks=None)

    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    decision = await acl.check_read_access(db, "ctx://datalake/prod/priority-test", ctx)
    assert decision.allowed is True
    assert decision.reason == "explicit allow"
    assert decision.field_masks is None


@pytest.mark.asyncio
async def test_a9_no_policy_fallback(acme_session, phase2_services):
    """A-9: No ACL policies → Phase 1 default behavior."""
    db = acme_session
    acl = phase2_services.acl
    from contexthub.models.request import RequestContext

    await _insert_context(db, "ctx://datalake/public/open-data",
                          scope="datalake", l1="open data")

    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    decision = await acl.check_read_access(db, "ctx://datalake/public/open-data", ctx)
    assert decision.allowed is True
    assert decision.reason == "default baseline"


@pytest.mark.asyncio
async def test_a10_audit_tier1(acme_session, phase2_services):
    """A-10: Tier-1 operations (create/delete/promote) leave audit records."""
    db = acme_session
    context_svc = phase2_services.context_svc
    from contexthub.models.context import CreateContextRequest
    from contexthub.models.request import RequestContext

    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    body = CreateContextRequest(
        uri="ctx://team/engineering/backend/audit-test",
        context_type="resource",
        scope="team",
        owner_space="engineering/backend",
        l1_content="audit test content",
    )
    await context_svc.create(db, body, ctx)

    count = await _count_audit(db, action="create",
                                resource_uri="ctx://team/engineering/backend/audit-test")
    assert count >= 1


@pytest.mark.asyncio
async def test_a10b_audit_tier2(acme_session, phase2_services):
    """A-10b: Tier-2 operations (read/search/ls/stat) leave audit records (best-effort)."""
    db = acme_session
    store = phase2_services.context_store
    from contexthub.models.context import ContextLevel
    from contexthub.models.request import RequestContext

    await _insert_context(db, "ctx://datalake/public/tier2-test",
                          scope="datalake", l1="tier2 audit test")

    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    await store.read(db, "ctx://datalake/public/tier2-test", ContextLevel.L1, ctx)

    count = await _count_audit(db, action="read",
                                resource_uri="ctx://datalake/public/tier2-test")
    assert count >= 1


@pytest.mark.asyncio
async def test_a11_acl_deny_audit(acme_session, phase2_services):
    """A-11: ACL deny produces an audit record with result='denied'."""
    db = acme_session
    acl = phase2_services.acl
    store = phase2_services.context_store
    from contexthub.models.context import ContextLevel
    from contexthub.models.request import RequestContext

    await _insert_context(db, "ctx://datalake/prod/denied-resource",
                          scope="datalake", l1="secret data")
    await _insert_policy(db, "ctx://datalake/prod/denied-resource",
                         "engineering/backend", "deny")

    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    try:
        await store.read(db, "ctx://datalake/prod/denied-resource", ContextLevel.L1, ctx)
    except Exception:
        pass

    count = await _count_audit(db, action="access_denied",
                                resource_uri="ctx://datalake/prod/denied-resource",
                                result="denied")
    assert count >= 1


@pytest.mark.asyncio
async def test_a12_share_grant(acme_session, phase2_services):
    """A-12: Share grant enables target principal to read the source."""
    db = acme_session
    share = phase2_services.share
    acl = phase2_services.acl
    from contexthub.models.request import RequestContext

    await _insert_context(db, "ctx://team/engineering/backend/configs/db-config",
                          scope="team", owner_space="engineering/backend",
                          l1="host=prod.db port=5432")

    ctx_owner = RequestContext(account_id="acme", agent_id="query-agent")
    ctx_target = RequestContext(account_id="acme", agent_id="analysis-agent")

    decision_before = await acl.check_read_access(
        db, "ctx://team/engineering/backend/configs/db-config", ctx_target
    )
    assert decision_before.allowed is False

    policy = await share.grant(
        db, "ctx://team/engineering/backend/configs/db-config",
        "data/analytics", ctx_owner
    )
    assert policy.effect == "allow"
    assert policy.conditions == {"kind": "share_grant"}

    decision_after = await acl.check_read_access(
        db, "ctx://team/engineering/backend/configs/db-config", ctx_target
    )
    assert decision_after.allowed is True
    assert decision_after.reason == "explicit allow"


@pytest.mark.asyncio
async def test_a13_share_revoke(acme_session, phase2_services):
    """A-13: Revoking a share grant removes read access."""
    db = acme_session
    share = phase2_services.share
    acl = phase2_services.acl
    from contexthub.models.request import RequestContext

    await _insert_context(db, "ctx://team/engineering/backend/configs/revoke-test",
                          scope="team", owner_space="engineering/backend",
                          l1="revoke test data")

    ctx_owner = RequestContext(account_id="acme", agent_id="query-agent")
    ctx_target = RequestContext(account_id="acme", agent_id="analysis-agent")

    policy = await share.grant(
        db, "ctx://team/engineering/backend/configs/revoke-test",
        "data/analytics", ctx_owner
    )

    decision = await acl.check_read_access(
        db, "ctx://team/engineering/backend/configs/revoke-test", ctx_target
    )
    assert decision.allowed is True

    await share.revoke(db, policy.id, ctx_owner)

    decision_after = await acl.check_read_access(
        db, "ctx://team/engineering/backend/configs/revoke-test", ctx_target
    )
    assert decision_after.allowed is False


@pytest.mark.asyncio
async def test_a14_share_no_copy(acme_session, phase2_services):
    """A-14: Source content changes are visible to the share target (no copy).

    ContextStore.read() returns a str, not a dict.
    """
    db = acme_session
    share = phase2_services.share
    store = phase2_services.context_store
    from contexthub.models.context import ContextLevel
    from contexthub.models.request import RequestContext

    await _insert_context(db, "ctx://team/engineering/backend/configs/live-data",
                          scope="team", owner_space="engineering/backend",
                          l1="original content")

    ctx_owner = RequestContext(account_id="acme", agent_id="query-agent")
    ctx_target = RequestContext(account_id="acme", agent_id="analysis-agent")

    await share.grant(
        db, "ctx://team/engineering/backend/configs/live-data",
        "data/analytics", ctx_owner
    )

    await db.execute(
        "UPDATE contexts SET l1_content = $1 WHERE uri = $2",
        "updated content",
        "ctx://team/engineering/backend/configs/live-data",
    )

    content = await store.read(
        db, "ctx://team/engineering/backend/configs/live-data", ContextLevel.L1, ctx_target
    )
    assert content == "updated content"


@pytest.mark.asyncio
async def test_a15_write_not_affected(acme_session, phase2_services):
    """A-15: Deny read policy does not affect write operations."""
    db = acme_session
    acl = phase2_services.acl
    from contexthub.models.request import RequestContext

    await _insert_context(db, "ctx://team/engineering/backend/write-test",
                          scope="team", owner_space="engineering/backend",
                          l1="write test")

    await _insert_policy(db, "ctx://team/engineering/backend/write-test",
                         "engineering/backend", "deny", actions=["read"])

    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    can_write = await acl.check_write(
        db, "ctx://team/engineering/backend/write-test", ctx
    )
    assert can_write is True


# ── Admin API HTTP Smoke Tests ──────────────────────────────────────────


@pytest_asyncio.fixture
async def http_client(db_pool, repo, clean_db, phase2_services):
    """AsyncClient connected to a lightweight FastAPI app with admin router only."""
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    from contexthub.api.routers.admin import router as admin_router

    test_app = FastAPI()
    test_app.include_router(admin_router)
    test_app.state.repo = repo
    test_app.state.audit_service = phase2_services.audit
    test_app.state.share_service = phase2_services.share
    test_app.state.acl_service = phase2_services.acl

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        yield client


ADMIN_HEADERS = {"X-Account-Id": "acme", "X-Agent-Id": "query-agent"}
NON_ADMIN_HEADERS = {"X-Account-Id": "acme", "X-Agent-Id": "analysis-agent"}

_FAKE_ID = "00000000-0000-0000-0000-000000000099"


@pytest.mark.parametrize("method,path,json_body", [
    ("POST", "/api/v1/admin/policies",
     {"resource_uri_pattern": "ctx://x/*", "principal": "a", "effect": "deny", "actions": ["read"]}),
    ("GET", "/api/v1/admin/policies", None),
    ("GET", f"/api/v1/admin/policies/{_FAKE_ID}", None),
    ("PATCH", f"/api/v1/admin/policies/{_FAKE_ID}", {"priority": 1}),
    ("DELETE", f"/api/v1/admin/policies/{_FAKE_ID}", None),
    ("GET", "/api/v1/admin/audit", None),
])
@pytest.mark.asyncio
async def test_http_admin_endpoints_require_admin(http_client, method, path, json_body):
    """Non-admin agent → 403 for all admin endpoints."""
    resp = await http_client.request(method, path, headers=NON_ADMIN_HEADERS, json=json_body)
    assert resp.status_code == 403, f"{method} {path} should return 403 for non-admin"


@pytest.mark.asyncio
async def test_http_policy_crud_lifecycle(http_client, db_pool):
    """Admin agent complete CRUD: create → list → get → update → delete."""
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO team_memberships (agent_id, team_id, role, access)
            VALUES ('query-agent', '00000000-0000-0000-0000-000000000003', 'admin', 'read_write')
            ON CONFLICT (agent_id, team_id) DO UPDATE SET role = 'admin'
        """)

    # CREATE
    resp = await http_client.post(
        "/api/v1/admin/policies",
        headers=ADMIN_HEADERS,
        json={
            "resource_uri_pattern": "ctx://datalake/prod/secret/*",
            "principal": "query-agent",
            "effect": "deny",
            "actions": ["read"],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "id" in body
    assert body["effect"] == "deny"
    policy_id = body["id"]

    # LIST
    resp = await http_client.get("/api/v1/admin/policies", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert any(p["id"] == policy_id for p in resp.json())

    # GET
    resp = await http_client.get(f"/api/v1/admin/policies/{policy_id}", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["id"] == policy_id

    # UPDATE
    resp = await http_client.patch(
        f"/api/v1/admin/policies/{policy_id}",
        headers=ADMIN_HEADERS,
        json={"priority": 10},
    )
    assert resp.status_code == 200
    assert resp.json()["priority"] == 10

    # DELETE
    resp = await http_client.delete(f"/api/v1/admin/policies/{policy_id}", headers=ADMIN_HEADERS)
    assert resp.status_code == 204

    # Verify deletion
    resp = await http_client.get(f"/api/v1/admin/policies/{policy_id}", headers=ADMIN_HEADERS)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_http_audit_query(http_client, db_pool):
    """Admin agent queries audit log → 200."""
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO team_memberships (agent_id, team_id, role, access)
            VALUES ('query-agent', '00000000-0000-0000-0000-000000000003', 'admin', 'read_write')
            ON CONFLICT (agent_id, team_id) DO UPDATE SET role = 'admin'
        """)

    resp = await http_client.get("/api/v1/admin/audit", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_http_share_grant_lifecycle(http_client, db_pool):
    """Create share grant → list → revoke, full lifecycle."""
    async with db_pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.account_id', 'acme', false)")
        await conn.execute("""
            INSERT INTO contexts (uri, context_type, scope, owner_space, account_id,
                                  l1_content)
            VALUES ('ctx://team/engineering/backend/configs/test-share',
                    'resource', 'team', 'engineering/backend', 'acme',
                    'test config content')
            ON CONFLICT (account_id, uri) DO NOTHING
        """)

    resp = await http_client.post(
        "/api/v1/shares",
        headers=ADMIN_HEADERS,
        json={
            "source_uri": "ctx://team/engineering/backend/configs/test-share",
            "target_principal": "data/analytics",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["conditions"] == {"kind": "share_grant"}
    grant_id = body["id"]

    # LIST
    resp = await http_client.get(
        "/api/v1/shares",
        headers=ADMIN_HEADERS,
        params={"source_uri": "ctx://team/engineering/backend/configs/test-share"},
    )
    assert resp.status_code == 200
    assert any(g["id"] == grant_id for g in resp.json())

    # REVOKE
    resp = await http_client.delete(f"/api/v1/shares/{grant_id}", headers=ADMIN_HEADERS)
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_http_list_policies_includes_share_grants(http_client, db_pool):
    """GET /admin/policies must include share grant rows (product decision)."""
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO team_memberships (agent_id, team_id, role, access)
            VALUES ('query-agent', '00000000-0000-0000-0000-000000000003', 'admin', 'read_write')
            ON CONFLICT (agent_id, team_id) DO UPDATE SET role = 'admin'
        """)
        await conn.execute("SELECT set_config('app.account_id', 'acme', false)")
        await conn.execute("""
            INSERT INTO contexts (uri, context_type, scope, owner_space, account_id, l1_content)
            VALUES ('ctx://team/engineering/backend/configs/share-visible',
                    'resource', 'team', 'engineering/backend', 'acme',
                    'share visibility test')
            ON CONFLICT (account_id, uri) DO NOTHING
        """)

    resp = await http_client.post(
        "/api/v1/shares",
        headers=ADMIN_HEADERS,
        json={
            "source_uri": "ctx://team/engineering/backend/configs/share-visible",
            "target_principal": "data/analytics",
        },
    )
    assert resp.status_code == 201
    grant_id = resp.json()["id"]

    resp = await http_client.get("/api/v1/admin/policies", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    policies = resp.json()
    share_grant = next((p for p in policies if p["id"] == grant_id), None)
    assert share_grant is not None, \
        "list_policies must include share grant rows — see product decision in GET /admin/policies"
    assert share_grant["conditions"] == {"kind": "share_grant"}


# ── main.py route registration test ────────────────────────────────────


def test_admin_router_registered_in_main():
    """Verify main.py registered all admin router (path, method) pairs."""
    from contexthub.main import app

    routes = {
        (getattr(r, "path", ""), m)
        for r in app.routes
        for m in getattr(r, "methods", set())
    }

    expected = [
        ("/api/v1/admin/policies", "POST"),
        ("/api/v1/admin/policies", "GET"),
        ("/api/v1/admin/policies/{policy_id}", "GET"),
        ("/api/v1/admin/policies/{policy_id}", "PATCH"),
        ("/api/v1/admin/policies/{policy_id}", "DELETE"),
        ("/api/v1/admin/audit", "GET"),
        ("/api/v1/shares", "POST"),
        ("/api/v1/shares", "GET"),
        ("/api/v1/shares/{policy_id}", "DELETE"),
    ]
    for path, method in expected:
        assert (path, method) in routes, \
            f"({method} {path}) not registered in main.py"


# ── SDK structure tests (no DB needed) ──────────────────────────────────


def test_sdk_admin_namespace_exists():
    """Verify SDK client has admin and share namespaces."""
    from contexthub_sdk import ContextHubClient
    import inspect

    init_src = inspect.getsource(ContextHubClient.__init__)
    assert "self.admin" in init_src
    assert "self.share" in init_src


def test_sdk_models_importable():
    """Verify new SDK models can be imported."""
    from contexthub_sdk import (
        AccessPolicyRecord,
        AuditEntryRecord,
        PolicyEffect,
        PolicyAction,
        AuditAction,
        AuditResult,
    )
    assert PolicyEffect.ALLOW.value == "allow"
    assert PolicyEffect.DENY.value == "deny"
    assert AuditAction.POLICY_CHANGE.value == "policy_change"
    assert AuditResult.DENIED.value == "denied"


def test_sdk_delete_patch_accept_optional_version():
    """Verify _delete and _patch expected_version defaults to None."""
    import inspect
    from contexthub_sdk import ContextHubClient

    delete_sig = inspect.signature(ContextHubClient._delete)
    assert delete_sig.parameters["expected_version"].default is None

    patch_sig = inspect.signature(ContextHubClient._patch)
    assert patch_sig.parameters["expected_version"].default is None


# ── Regression test ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_regression_phase1_default_visibility(acme_session, phase2_services):
    """No ACL policies → Phase 1 default visibility behavior unchanged."""
    db = acme_session
    acl = phase2_services.acl
    from contexthub.models.request import RequestContext

    await _insert_context(db, "ctx://datalake/public/data", scope="datalake", l1="public data")
    ctx = RequestContext(account_id="acme", agent_id="query-agent")
    decision = await acl.check_read_access(db, "ctx://datalake/public/data", ctx)
    assert decision.allowed is True
    assert decision.reason == "default baseline"
    assert decision.field_masks is None
