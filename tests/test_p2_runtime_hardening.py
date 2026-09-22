"""Focused crash, lease, idempotency, and multi-worker P2 runtime tests."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from contexthub.propagation.registry import PropagationRuleRegistry
from contexthub.services.propagation_engine import PropagationEngine


class StatefulOutbox:
    def __init__(self):
        self.events: dict = {}
        self.effects: dict[tuple, dict] = {}
        self.risk: set[tuple] = set()
        self.trace: list[tuple] = []
        self._lock = asyncio.Lock()

    def enqueue(self, *, status="pending", claimed_at=None, attempt=0):
        event_id = uuid4()
        context_id = uuid4()
        self.events[event_id] = {
            "event_id": event_id,
            "context_id": context_id,
            "account_id": "acme",
            "change_type": "created",
            "timestamp": datetime.now(timezone.utc),
            "delivery_status": status,
            "attempt_count": attempt,
            "claimed_at": claimed_at,
            "heartbeat_at": claimed_at,
            "next_retry_at": datetime.now(timezone.utc),
            "lease_token": None,
            "source_version": 1,
            "depth": 0,
            "metadata": {},
        }
        return event_id

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchval(self, sql, *args):
        if "SELECT 1 FROM change_events" in sql:
            event = self.events[args[0]]
            return (
                1
                if event["delivery_status"] == "processing"
                and event["lease_token"] == args[1]
                else None
            )
        return None

    async def fetch(self, sql, *args):
        if "WITH ready AS" in sql:
            context_id, limit, token, owner = args
            async with self._lock:
                rows = [
                    event
                    for event in self.events.values()
                    if event["delivery_status"] in ("pending", "retry")
                    and (context_id is None or str(event["context_id"]) == str(context_id))
                ][:limit]
                for event in rows:
                    event.update(
                        delivery_status="processing",
                        lease_token=token,
                        lease_owner=owner,
                        claimed_at=datetime.now(timezone.utc),
                        heartbeat_at=datetime.now(timezone.utc),
                        attempt_count=event["attempt_count"] + 1,
                    )
                return [dict(row) for row in rows]
        if "FROM dependencies" in sql or "FROM skill_subscriptions" in sql:
            return []
        if "delivery_status NOT IN ('processed', 'succeeded')" in sql:
            return [
                {
                    "event_id": event["event_id"],
                    "delivery_status": event["delivery_status"],
                }
                for event in self.events.values()
                if event["delivery_status"] not in ("processed", "succeeded")
                and (args[0] is None or str(event["context_id"]) == str(args[0]))
            ]
        return []

    async def fetchrow(self, sql, *args):
        if "INSERT INTO propagation_effects" in sql:
            key = (args[0], args[1])
            current = self.effects.get(key)
            if current and current["status"] in ("started", "succeeded"):
                return None
            self.effects[key] = {"status": "started", "lease_token": args[5]}
            return {"status": "started"}
        return None

    async def execute(self, sql, *args):
        if "processing lease expired" in sql:
            cutoff = datetime.now(timezone.utc) - args[0]
            for event in self.events.values():
                heartbeat = event.get("heartbeat_at") or event.get("claimed_at")
                if event["delivery_status"] == "processing" and heartbeat < cutoff:
                    event.update(
                        delivery_status="retry",
                        lease_token=None,
                        claimed_at=None,
                        heartbeat_at=None,
                    )
            return "UPDATE 1"
        if "SET heartbeat_at = NOW()" in sql:
            event = self.events[args[0]]
            if (
                event["delivery_status"] == "processing"
                and event["lease_token"] == args[1]
            ):
                event["heartbeat_at"] = datetime.now(timezone.utc)
                return "UPDATE 1"
            return "UPDATE 0"
        if "SET delivery_status = 'succeeded'" in sql:
            event = self.events[args[0]]
            if args[1] is None or event["lease_token"] == args[1]:
                event.update(delivery_status="succeeded", lease_token=None)
                return "UPDATE 1"
            return "UPDATE 0"
        if "SET delivery_status = CASE" in sql:
            event = self.events[args[0]]
            event["delivery_status"] = "dead_letter" if args[2] else "retry"
            event["last_error"] = args[3]
            event["lease_token"] = None
            return "UPDATE 1"
        if "UPDATE propagation_effects" in sql:
            self.effects[(args[0], args[1])] = {
                "status": args[2],
                "result": args[3],
            }
            return "UPDATE 1"
        if "INSERT INTO propagation_trace" in sql:
            self.trace.append(args)
            return "INSERT 1"
        if "INSERT INTO propagation_risk_ledger" in sql:
            self.risk.add((args[0], args[1]))
            return "INSERT 1"
        return "UPDATE 1"


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class Repo:
    @asynccontextmanager
    async def session(self, account):
        yield self


def engine(store, worker):
    return PropagationEngine(
        repo=Repo(),
        pool=Pool(store),
        dsn="postgresql://unused",
        rule_registry=PropagationRuleRegistry.default(),
        lifecycle=MagicMock(),
        lease_timeout=5,
        worker_id=worker,
    )


@pytest.mark.asyncio
async def test_committed_event_survives_worker_restart():
    store = StatefulOutbox()
    event_id = store.enqueue()
    report = await engine(store, "after-restart").drain_once()
    assert store.events[event_id]["delivery_status"] == "succeeded"
    assert report.complete


@pytest.mark.asyncio
async def test_stale_processing_lease_is_recovered_after_crash():
    store = StatefulOutbox()
    event_id = store.enqueue(
        status="processing",
        claimed_at=datetime.now(timezone.utc) - timedelta(seconds=30),
        attempt=1,
    )
    report = await engine(store, "takeover").drain_once()
    assert store.events[event_id]["delivery_status"] == "succeeded"
    assert store.events[event_id]["attempt_count"] == 2
    assert report.complete


@pytest.mark.asyncio
async def test_duplicate_event_effect_executes_once():
    store = StatefulOutbox()
    store.enqueue()
    event = (await engine(store, "one")._claim_ready_events(None, 1))[0]
    first = await engine(store, "one")._claim_effect(
        event,
        effect_key="dependency:x:y:1",
        effect_type="dependency",
        target_context_id=uuid4(),
    )
    second = await engine(store, "two")._claim_effect(
        event,
        effect_key="dependency:x:y:1",
        effect_type="dependency",
        target_context_id=uuid4(),
    )
    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_duplicate_delivery_does_not_double_charge_risk():
    store = StatefulOutbox()
    event_id = store.enqueue(status="processing")
    event = store.events[event_id]
    dependent = uuid4()
    dep = {"dependent_id": dependent}
    edge = f"{event['context_id']}->{dependent}"
    event["plan_id"] = uuid4()
    event["graph_scope"] = "graph-v1"
    event["metadata"] = {
        "plan_required": True,
        "assignments": {edge: "J3"},
        "risk_delta_by_edge": {edge: 0.1},
    }
    runtime = engine(store, "worker")
    await runtime._record_risk_once(event, dep)
    await runtime._record_risk_once(event, dep)
    assert store.risk == {(event_id, edge)}


@pytest.mark.asyncio
async def test_planned_edge_without_assignment_or_risk_fails_closed():
    store = StatefulOutbox()
    event_id = store.enqueue(status="processing")
    event = store.events[event_id]
    event.update(
        plan_id=uuid4(),
        graph_scope="graph-v1",
        metadata={"plan_required": True, "assignments": {}, "risk_delta_by_edge": {}},
    )
    with pytest.raises(RuntimeError, match="incomplete planned risk metadata"):
        await engine(store, "worker")._record_risk_once(
            event, {"dependent_id": uuid4()}
        )


@pytest.mark.asyncio
async def test_child_frontier_inherits_plan_and_duplicate_event_is_charged_once():
    store = StatefulOutbox()
    child_event_id = store.enqueue(status="processing")
    child = store.events[child_event_id]
    dependent = uuid4()
    edge = f"{child['context_id']}->{dependent}"
    child.update(
        plan_id=uuid4(),
        graph_scope="graph-v1",
        parent_event_id=uuid4(),
        metadata={
            "plan_required": True,
            "assignments": {edge: "direct-stale"},
            "risk_delta_by_edge": {edge: 0.0},
        },
    )
    runtime = engine(store, "worker")
    await runtime._record_risk_once(child, {"dependent_id": dependent})
    await runtime._record_risk_once(child, {"dependent_id": dependent})
    assert store.risk == {(child_event_id, edge)}


@pytest.mark.asyncio
async def test_two_workers_compete_without_double_claim():
    store = StatefulOutbox()
    store.enqueue()
    first, second = await asyncio.gather(
        engine(store, "one")._claim_ready_events(None, 1),
        engine(store, "two")._claim_ready_events(None, 1),
    )
    assert sorted((len(first), len(second))) == [0, 1]


@pytest.mark.asyncio
async def test_old_worker_stops_after_takeover_and_cannot_write_success_trace():
    store = StatefulOutbox()
    event_id = store.enqueue()
    stale_event = (await engine(store, "old")._claim_ready_events(None, 1))[0]
    store.events[event_id].update(delivery_status="retry", lease_token=None)
    fresh_event = (await engine(store, "new")._claim_ready_events(None, 1))[0]
    from contexthub.services.propagation_engine import LeaseLostError

    with pytest.raises(LeaseLostError):
        await engine(store, "old")._heartbeat(stale_event)
    before = len(store.trace)
    with pytest.raises(LeaseLostError):
        await engine(store, "old")._finish_event(
            event_id,
            success=True,
            lease_token=stale_event["lease_token"],
        )
    assert len(store.trace) == before
    assert store.events[event_id]["lease_token"] == fresh_event["lease_token"]


@pytest.mark.asyncio
async def test_retryable_and_terminal_failure_are_explicit_leftovers():
    store = StatefulOutbox()
    event_id = store.enqueue(status="processing", attempt=1)
    runtime = engine(store, "worker")
    await runtime._finish_event(event_id, success=False, error="temporary")
    report = await runtime.drain_once(limit=0)
    assert store.events[event_id]["delivery_status"] == "retry"
    assert report.unfinished == 1
    await runtime._finish_event(
        event_id, success=False, terminal=True, error="invalid plan"
    )
    report = await runtime.drain_once(limit=0)
    assert store.events[event_id]["delivery_status"] == "dead_letter"
    assert report.failed == 1
    assert not report.complete
