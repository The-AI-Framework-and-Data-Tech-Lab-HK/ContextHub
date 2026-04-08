"""Unit tests for Phase 2 ACL read-path evaluation engine.

Tests cover check_read_access(), filter_visible_with_acl(), and
_match_uri_pattern() using a filtering mock DB — no real Postgres needed.
Scenarios track task-prompt §8.1–§8.10.

The MockDB filters policies by URI pattern, principal, and action to mirror
what the real SQL would do, so tests can include "noise" policies and verify
they are correctly excluded.  Actual SQL LIKE + ESCAPE behaviour is deferred
to Task 6 integration tests against a real Postgres instance.
"""

from __future__ import annotations

import uuid

import pytest

from contexthub.models.context import Scope
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeRecord:
    """Minimal asyncpg.Record stand-in supporting dict() conversion."""

    def __init__(self, **data):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]

    def keys(self):
        return self._data.keys()


def _policy(
    *,
    effect: str,
    principal: str,
    pattern: str,
    actions: list[str] | None = None,
    field_masks: list[str] | None = None,
    priority: int = 0,
) -> dict:
    """Build a dict that looks like an access_policies row."""
    return {
        "id": uuid.uuid4(),
        "resource_uri_pattern": pattern,
        "principal": principal,
        "effect": effect,
        "actions": actions or ["read"],
        "conditions": None,
        "field_masks": field_masks,
        "priority": priority,
        "account_id": "acme",
        "created_at": None,
        "updated_at": None,
        "created_by": None,
    }


def _sort_policies(policies: list[dict]) -> list[dict]:
    """deny-first, then priority DESC — mirrors the SQL ORDER BY."""
    return sorted(
        policies,
        key=lambda p: (0 if p["effect"] == "deny" else 1, -p["priority"]),
    )


class MockDB:
    """Fake ScopedRepo that filters policies like the real SQL would.

    Instead of blindly returning all policies, the mock applies the same
    filtering criteria as the SQL queries (URI pattern match via
    ``_match_uri_pattern``, principal membership, action inclusion) and
    sorts results deny-first + priority DESC.

    This catches issues where a policy with the wrong principal / URI /
    action accidentally leaks into the evaluation logic.
    """

    def __init__(
        self,
        *,
        context: dict | None = None,
        visible_paths: list[str] | None = None,
        direct_paths: list[str] | None = None,
        policies: list[dict] | None = None,
    ):
        self._ctx = context
        self._visible = visible_paths or []
        self._direct = direct_paths or []
        self._policies = policies or []

    # -- ScopedRepo interface stubs --

    async def fetchrow(self, sql: str, *args):
        if "FROM contexts" in sql:
            return FakeRecord(**self._ctx) if self._ctx else None
        raise AssertionError(f"Unexpected fetchrow: {sql}")

    async def fetch(self, sql: str, *args):
        if "visible_teams" in sql:
            return [FakeRecord(path=p) for p in self._visible]
        if "SELECT t.path FROM teams t" in sql:
            return [FakeRecord(path=p) for p in self._direct]
        if "access_policies" in sql:
            if "LIKE" in sql:
                return self._filter_matching(args)
            return self._filter_all_read(args)
        raise AssertionError(f"Unexpected fetch: {sql}")

    async def fetchval(self, sql: str, *args):
        if "access_policies" in sql:
            return self._hierarchy_deny_check(args)
        if "team_memberships" in sql:
            return None
        raise AssertionError(f"Unexpected fetchval: {sql}")

    # -- filtering helpers --

    def _filter_matching(self, args) -> list[FakeRecord]:
        """Simulate _fetch_matching_policies SQL: filter by URI + principal + action."""
        uri, agent_id, team_paths, action = args
        matched = [
            p for p in self._policies
            if ACLService._match_uri_pattern(uri, p["resource_uri_pattern"])
            and _principal_matches(p["principal"], agent_id, team_paths)
            and action in p["actions"]
        ]
        return [FakeRecord(**p) for p in _sort_policies(matched)]

    def _filter_all_read(self, args) -> list[FakeRecord]:
        """Simulate _fetch_all_read_policies SQL: filter by principal + 'read' action (no URI)."""
        agent_id, team_paths = args
        matched = [
            p for p in self._policies
            if _principal_matches(p["principal"], agent_id, team_paths)
            and "read" in p["actions"]
        ]
        return [FakeRecord(**p) for p in _sort_policies(matched)]

    def _hierarchy_deny_check(self, args):
        """Simulate _check_hierarchy_deny fetchval: ancestor deny exists?"""
        ancestor_paths, uri, action = args
        for p in self._policies:
            if (
                p["principal"] in ancestor_paths
                and p["effect"] == "deny"
                and ACLService._match_uri_pattern(uri, p["resource_uri_pattern"])
                and action in p["actions"]
            ):
                return 1
        return None


def _principal_matches(principal: str, agent_id: str, team_paths: list[str]) -> bool:
    return principal == agent_id or principal in team_paths


CTX_AGENT_1 = RequestContext(account_id="acme", agent_id="agent-1")
CTX_BACKEND = RequestContext(account_id="acme", agent_id="backend-agent")


# ===================================================================
# _match_uri_pattern — pure function tests
# ===================================================================

class TestMatchUriPattern:
    def test_exact_match(self):
        assert ACLService._match_uri_pattern(
            "ctx://datalake/prod/orders", "ctx://datalake/prod/orders"
        )

    def test_exact_no_match(self):
        assert not ACLService._match_uri_pattern(
            "ctx://datalake/prod/orders", "ctx://datalake/prod/other"
        )

    def test_wildcard(self):
        assert ACLService._match_uri_pattern(
            "ctx://datalake/prod/orders", "ctx://datalake/prod/*"
        )

    def test_wildcard_nested(self):
        assert ACLService._match_uri_pattern(
            "ctx://datalake/prod/salary/details", "ctx://datalake/prod/*"
        )

    def test_wildcard_no_match(self):
        assert not ACLService._match_uri_pattern(
            "ctx://datalake/staging/orders", "ctx://datalake/prod/*"
        )

    def test_underscore_is_literal(self):
        """_ must not act as a single-char wildcard (the fixed SQL bug)."""
        assert ACLService._match_uri_pattern(
            "ctx://datalake/shared_knowledge",
            "ctx://datalake/shared_knowledge",
        )
        assert not ACLService._match_uri_pattern(
            "ctx://datalake/sharedXknowledge",
            "ctx://datalake/shared_knowledge",
        )

    def test_percent_is_literal(self):
        assert ACLService._match_uri_pattern(
            "ctx://datalake/100%done", "ctx://datalake/100%done"
        )
        assert not ACLService._match_uri_pattern(
            "ctx://datalake/100Xdone", "ctx://datalake/100%done"
        )


# ===================================================================
# §8.1  deny-override
# ===================================================================

@pytest.mark.asyncio
async def test_deny_override():
    """deny + allow on same URI → deny wins regardless of priority."""
    db = MockDB(
        context={"scope": "datalake", "owner_space": None},
        visible_paths=["engineering/backend", "engineering", ""],
        direct_paths=["engineering/backend"],
        policies=[
            _policy(effect="deny", principal="agent-1",
                    pattern="ctx://datalake/prod/secret"),
            _policy(effect="allow", principal="agent-1",
                    pattern="ctx://datalake/prod/secret", priority=100),
            # noise: different principal — must be filtered out
            _policy(effect="allow", principal="other-agent",
                    pattern="ctx://datalake/prod/secret", priority=200),
        ],
    )
    decision = await ACLService().check_read_access(
        db, "ctx://datalake/prod/secret", CTX_AGENT_1
    )
    assert decision.allowed is False
    assert decision.reason == "explicit deny"


# ===================================================================
# §8.2  team hierarchy deny
# ===================================================================

@pytest.mark.asyncio
async def test_parent_team_deny():
    """Parent team deny cannot be overridden by child team allow (§8.2A)."""
    db = MockDB(
        context={"scope": "datalake", "owner_space": None},
        visible_paths=["engineering/backend", "engineering", ""],
        direct_paths=["engineering/backend"],
        policies=[
            _policy(effect="deny", principal="engineering",
                    pattern="ctx://datalake/prod/salary/*"),
            _policy(effect="allow", principal="engineering/backend",
                    pattern="ctx://datalake/prod/salary/*"),
        ],
    )
    decision = await ACLService().check_read_access(
        db, "ctx://datalake/prod/salary/details", CTX_BACKEND
    )
    assert decision.allowed is False
    assert decision.reason == "parent team deny"


@pytest.mark.asyncio
async def test_root_team_deny():
    """Root team (path='') deny cannot be overridden (§8.2B)."""
    db = MockDB(
        context={"scope": "datalake", "owner_space": None},
        visible_paths=["engineering/backend", "engineering", ""],
        direct_paths=["engineering/backend"],
        policies=[
            _policy(effect="deny", principal="",
                    pattern="ctx://datalake/prod/salary/*"),
            _policy(effect="allow", principal="engineering/backend",
                    pattern="ctx://datalake/prod/salary/*"),
        ],
    )
    decision = await ACLService().check_read_access(
        db, "ctx://datalake/prod/salary/details", CTX_BACKEND
    )
    assert decision.allowed is False
    assert decision.reason == "parent team deny"


# ===================================================================
# §8.3  no matching policies → default baseline
# ===================================================================

@pytest.mark.asyncio
async def test_baseline_allowed_no_policies():
    """Datalake scope, no ACL → baseline allows."""
    db = MockDB(
        context={"scope": "datalake", "owner_space": None},
        visible_paths=["engineering"],
        direct_paths=["engineering"],
    )
    decision = await ACLService().check_read_access(
        db, "ctx://datalake/public/data", CTX_AGENT_1
    )
    assert decision.allowed is True
    assert decision.reason == "default baseline"
    assert decision.field_masks is None


@pytest.mark.asyncio
async def test_baseline_denied_no_policies():
    """Team scope, agent not in team, no ACL → baseline denies."""
    db = MockDB(
        context={"scope": "team", "owner_space": "hr"},
        visible_paths=["engineering"],
        direct_paths=["engineering"],
    )
    decision = await ACLService().check_read_access(
        db, "ctx://team/hr/salaries", CTX_AGENT_1
    )
    assert decision.allowed is False
    assert decision.reason == "default baseline"


# ===================================================================
# §8.4  URI pattern matching (wildcard hit / miss)
# ===================================================================

@pytest.mark.asyncio
async def test_wildcard_deny_matches():
    """Wildcard deny matches sub-path URI."""
    db = MockDB(
        context={"scope": "datalake", "owner_space": None},
        visible_paths=[],
        direct_paths=[],
        policies=[
            _policy(effect="deny", principal="agent-1",
                    pattern="ctx://datalake/prod/*"),
        ],
    )
    decision = await ACLService().check_read_access(
        db, "ctx://datalake/prod/orders", CTX_AGENT_1
    )
    assert decision.allowed is False
    assert decision.reason == "explicit deny"


@pytest.mark.asyncio
async def test_wildcard_deny_no_match_falls_to_baseline():
    """URI outside wildcard scope → policy not matched → baseline."""
    db = MockDB(
        context={"scope": "datalake", "owner_space": None},
        visible_paths=[],
        direct_paths=[],
        policies=[
            # present but won't match staging URI
            _policy(effect="deny", principal="agent-1",
                    pattern="ctx://datalake/prod/*"),
        ],
    )
    decision = await ACLService().check_read_access(
        db, "ctx://datalake/staging/orders", CTX_AGENT_1
    )
    assert decision.allowed is True
    assert decision.reason == "default baseline"


# ===================================================================
# §8.5  explicit allow on default-invisible resource
# ===================================================================

@pytest.mark.asyncio
async def test_explicit_allow_cross_team():
    """Allow policy grants read on a resource the agent cannot see by default."""
    db = MockDB(
        context={"scope": "team", "owner_space": "hr"},
        visible_paths=["engineering"],
        direct_paths=["engineering"],
        policies=[
            _policy(effect="allow", principal="agent-1",
                    pattern="ctx://team/hr/salaries"),
            # noise: matching principal but non-matching URI
            _policy(effect="deny", principal="agent-1",
                    pattern="ctx://team/finance/*"),
        ],
    )
    decision = await ACLService().check_read_access(
        db, "ctx://team/hr/salaries", CTX_AGENT_1
    )
    assert decision.allowed is True
    assert decision.reason == "explicit allow"


# ===================================================================
# §8.6  allow with field_masks
# ===================================================================

@pytest.mark.asyncio
async def test_allow_with_field_masks():
    """Allow policy carries field_masks through to the decision."""
    db = MockDB(
        context={"scope": "team", "owner_space": "hr"},
        visible_paths=["engineering"],
        direct_paths=["engineering"],
        policies=[
            _policy(effect="allow", principal="agent-1",
                    pattern="ctx://team/hr/*",
                    field_masks=["salary", "ssn"]),
        ],
    )
    decision = await ACLService().check_read_access(
        db, "ctx://team/hr/report", CTX_AGENT_1
    )
    assert decision.allowed is True
    assert decision.field_masks == ["salary", "ssn"]


# ===================================================================
# §8.8  priority resolution — single-item path
# ===================================================================

@pytest.mark.asyncio
async def test_highest_priority_allow_wins():
    """Two allows: higher priority's field_masks are used."""
    db = MockDB(
        context={"scope": "datalake", "owner_space": None},
        visible_paths=[],
        direct_paths=[],
        policies=[
            _policy(effect="allow", principal="agent-1",
                    pattern="ctx://datalake/prod/*",
                    field_masks=None, priority=10),
            _policy(effect="allow", principal="agent-1",
                    pattern="ctx://datalake/prod/*",
                    field_masks=["salary"], priority=5),
        ],
    )
    decision = await ACLService().check_read_access(
        db, "ctx://datalake/prod/report", CTX_AGENT_1
    )
    assert decision.allowed is True
    assert decision.field_masks is None  # priority=10 has no masks


# ===================================================================
# §8.10  role-type principal does not match
# ===================================================================

@pytest.mark.asyncio
async def test_role_principal_no_match():
    """A principal='admin' policy is present but filtered out by mock."""
    db = MockDB(
        context={"scope": "datalake", "owner_space": None},
        visible_paths=["engineering/backend", "engineering", ""],
        direct_paths=["engineering/backend"],
        policies=[
            _policy(effect="allow", principal="admin",
                    pattern="ctx://datalake/prod/*"),
        ],
    )
    decision = await ACLService().check_read_access(
        db, "ctx://datalake/prod/report", CTX_AGENT_1
    )
    assert decision.allowed is True
    assert decision.reason == "default baseline"


# ===================================================================
# §8.7  filter_visible_with_acl — batch
# ===================================================================

@pytest.mark.asyncio
async def test_filter_batch_deny_removes_allow_rescues():
    """deny removes default-visible; allow rescues default-invisible (§8.7)."""
    candidates = [
        {"uri": "ctx://team/engineering/doc1", "scope": "team",
         "owner_space": "engineering", "status": "active"},
        {"uri": "ctx://team/hr/doc2", "scope": "team",
         "owner_space": "hr", "status": "active"},
        {"uri": "ctx://team/engineering/doc3", "scope": "team",
         "owner_space": "engineering", "status": "active"},
    ]
    db = MockDB(
        visible_paths=["engineering"],
        direct_paths=["engineering"],
        policies=[
            _policy(effect="deny", principal="agent-1",
                    pattern="ctx://team/engineering/doc1"),
            _policy(effect="allow", principal="agent-1",
                    pattern="ctx://team/hr/doc2"),
            # noise: role-type principal, should be filtered out
            _policy(effect="allow", principal="admin",
                    pattern="ctx://team/*"),
        ],
    )
    acl = ACLService()
    results = await acl.filter_visible_with_acl(db, candidates, CTX_AGENT_1)

    uris = [r[0]["uri"] for r in results]
    assert "ctx://team/engineering/doc1" not in uris
    assert "ctx://team/hr/doc2" in uris
    assert "ctx://team/engineering/doc3" in uris

    masks_by_uri = {r[0]["uri"]: r[1] for r in results}
    assert masks_by_uri["ctx://team/hr/doc2"] is None
    assert masks_by_uri["ctx://team/engineering/doc3"] is None


@pytest.mark.asyncio
async def test_filter_batch_skips_deleted():
    """Deleted contexts are always excluded from results."""
    candidates = [
        {"uri": "ctx://datalake/data", "scope": "datalake",
         "owner_space": None, "status": "deleted"},
    ]
    db = MockDB(visible_paths=[], direct_paths=[])
    results = await ACLService().filter_visible_with_acl(
        db, candidates, CTX_AGENT_1
    )
    assert results == []


@pytest.mark.asyncio
async def test_filter_batch_hierarchy_deny():
    """Ancestor team deny in batch path removes the candidate."""
    candidates = [
        {"uri": "ctx://datalake/prod/salary/details", "scope": "datalake",
         "owner_space": None, "status": "active"},
    ]
    db = MockDB(
        visible_paths=["engineering/backend", "engineering", ""],
        direct_paths=["engineering/backend"],
        policies=[
            _policy(effect="deny", principal="engineering",
                    pattern="ctx://datalake/prod/salary/*"),
            _policy(effect="allow", principal="engineering/backend",
                    pattern="ctx://datalake/prod/salary/*"),
        ],
    )
    results = await ACLService().filter_visible_with_acl(
        db, candidates, CTX_BACKEND
    )
    assert results == []


@pytest.mark.asyncio
async def test_filter_batch_allow_with_field_masks():
    """Allow policy's field_masks propagate through batch filter."""
    candidates = [
        {"uri": "ctx://team/hr/report", "scope": "team",
         "owner_space": "hr", "status": "active"},
    ]
    db = MockDB(
        visible_paths=["engineering"],
        direct_paths=["engineering"],
        policies=[
            _policy(effect="allow", principal="agent-1",
                    pattern="ctx://team/hr/*",
                    field_masks=["salary", "ssn"]),
        ],
    )
    results = await ACLService().filter_visible_with_acl(
        db, candidates, CTX_AGENT_1
    )
    assert len(results) == 1
    assert results[0][1] == ["salary", "ssn"]


# ===================================================================
# §8.8 (batch)  priority resolution in filter_visible_with_acl
# ===================================================================

@pytest.mark.asyncio
async def test_filter_batch_priority_resolution():
    """In batch path, higher priority allow's field_masks are used."""
    candidates = [
        {"uri": "ctx://datalake/prod/report", "scope": "datalake",
         "owner_space": None, "status": "active"},
    ]
    db = MockDB(
        visible_paths=["engineering"],
        direct_paths=["engineering"],
        policies=[
            _policy(effect="allow", principal="agent-1",
                    pattern="ctx://datalake/prod/*",
                    field_masks=None, priority=10),
            _policy(effect="allow", principal="agent-1",
                    pattern="ctx://datalake/prod/*",
                    field_masks=["salary"], priority=5),
        ],
    )
    results = await ACLService().filter_visible_with_acl(
        db, candidates, CTX_AGENT_1
    )
    assert len(results) == 1
    assert results[0][1] is None  # priority=10 has no masks


# ===================================================================
# §8.9  write path not affected (Phase 1 methods untouched)
# ===================================================================

class _WritePathDB:
    """DB stub that explodes if access_policies is ever queried."""

    async def fetchrow(self, sql, *args):
        if "FROM contexts" in sql:
            return FakeRecord(scope="team", owner_space="engineering")
        raise AssertionError(f"Unexpected fetchrow: {sql}")

    async def fetch(self, sql, *args):
        if "access_policies" in sql:
            raise AssertionError("write path must not query access_policies")
        if "visible_teams" in sql:
            return [FakeRecord(path="engineering")]
        return []

    async def fetchval(self, sql, *args):
        if "access_policies" in sql:
            raise AssertionError("write path must not query access_policies")
        if "team_memberships" in sql:
            return 1
        return None


@pytest.mark.asyncio
async def test_check_write_does_not_query_access_policies():
    """check_write uses Phase 1 logic only."""
    result = await ACLService().check_write(
        _WritePathDB(), "ctx://team/engineering/doc", CTX_AGENT_1
    )
    assert result is True


@pytest.mark.asyncio
async def test_check_write_target_does_not_query_access_policies():
    """check_write_target uses Phase 1 logic only."""
    result = await ACLService().check_write_target(
        _WritePathDB(), Scope.TEAM, "engineering", CTX_AGENT_1
    )
    assert result is True
