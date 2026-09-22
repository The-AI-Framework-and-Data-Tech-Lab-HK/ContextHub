from uuid import uuid4

import pytest

from contexthub.models.request import RequestContext
from contexthub.services.lifecycle_service import LifecycleService


class RecordingDB:
    def __init__(self):
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return {
            "id": args[0],
            "uri": f"ctx://memory/{args[0]}",
            "status": "active",
            "version": 1,
        }

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return uuid4()

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "UPDATE 1"


@pytest.mark.asyncio
async def test_stale_source_invalidates_known_dependency_and_identity_closure():
    db = RecordingDB()
    await LifecycleService().mark_stale(
        db,
        uuid4(),
        "structured upstream invalidation",
        RequestContext(account_id="acme", agent_id="propagation_engine"),
    )
    closure_sql = next(
        sql for sql, _ in db.calls
        if "WITH RECURSIVE invalid" in sql
    )
    assert "dependencies" in closure_sql
    assert "context_relations" in closure_sql
    assert "'alias_of', 'duplicate_of', 'materialized_from'" in closure_sql
    assert "l0_content" not in closure_sql
    assert "l1_content" not in closure_sql
    assert "l2_content" not in closure_sql


@pytest.mark.asyncio
async def test_stale_event_and_closure_share_the_callers_transaction():
    db = RecordingDB()
    await LifecycleService().mark_stale(
        db,
        uuid4(),
        "reason",
        RequestContext(account_id="acme", agent_id="propagation_engine"),
    )
    sql = "\n".join(call[0] for call in db.calls)
    assert "UPDATE contexts" in sql
    assert "INSERT INTO change_events" in sql
    assert "INSERT INTO propagation_trace" in sql
    # LifecycleService receives one ScopedRepo; it never opens a second session.
    assert not hasattr(LifecycleService(), "_pool")
