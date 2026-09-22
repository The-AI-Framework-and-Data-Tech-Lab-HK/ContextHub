"""PropagationEngine: outbox consumer, retry/sweep scheduler."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import hashlib
import json
import logging
from datetime import timedelta
from uuid import uuid4

import asyncpg

from contexthub.db.repository import PgRepository, ScopedRepo
from contexthub.errors import NotFoundError
from contexthub.propagation.base import PropagationAction
from contexthub.propagation.registry import PropagationRuleRegistry
from contexthub.services.lifecycle_service import LifecycleService, make_system_context

logger = logging.getLogger(__name__)


class LeaseLostError(RuntimeError):
    """Raised as soon as a worker no longer owns an event/effect lease."""


def _affected_rows(command_tag: str) -> int:
    try:
        return int(command_tag.rsplit(" ", 1)[-1])
    except (AttributeError, TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class DrainReport:
    claimed: int
    succeeded: int
    retryable: int
    failed: int
    unfinished: int
    leftover_event_ids: tuple[str, ...]
    errors: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.unfinished == 0 and self.failed == 0 and not self.errors


class PropagationEngine:
    """Durable at-least-once outbox consumer; NOTIFY only wakes workers.

    三个入口共用同一条串行 drain 逻辑：
    - start()            → 启动后台 drain loop，并立刻唤醒一次 startup drain
    - _on_notify()       → 记录待优先处理的 context_id，并唤醒 drain loop
    - _periodic_wakeup() → 周期唤醒 drain loop，兜住漏通知和 crash 窗口

    Claims use ``FOR UPDATE SKIP LOCKED`` and a per-lease token. Durable
    ``propagation_effects`` make edge effects idempotent across duplicate
    delivery, process restart, and stale-lease takeover.
    """

    def __init__(
        self,
        repo: PgRepository,
        pool: asyncpg.Pool,
        dsn: str,
        rule_registry: PropagationRuleRegistry,
        lifecycle: LifecycleService,
        sweep_interval: int = 30,
        lease_timeout: int = 300,
        cascade_on_stale: bool = False,
        worker_id: str | None = None,
        max_event_depth: int = 64,
        max_events_per_root: int = 10_000,
    ):
        self._repo = repo
        self._pool = pool
        self._dsn = dsn
        self._registry = rule_registry
        self._lifecycle = lifecycle
        self._sweep_interval = sweep_interval
        self._lease_timeout = lease_timeout
        # 放行 marked_stale 传播以支持 derived_from 多 hop 级联（默认关闭）。
        self._cascade_on_stale = cascade_on_stale
        self._worker_id = worker_id or f"worker-{uuid4()}"
        self._max_event_depth = max_event_depth
        self._max_events_per_root = max_events_per_root
        self._listen_conn: asyncpg.Connection | None = None
        self._drain_task: asyncio.Task | None = None
        self._ticker_task: asyncio.Task | None = None
        self._wakeup = asyncio.Event()
        self._priority_context_ids: set[str] = set()
        self._running = False
    async def start(self) -> None:
        """启动传播引擎：建立 LISTEN 连接 + 启动串行 drain loop。"""
        if self._running:
            return

        listen_conn: asyncpg.Connection | None = None
        drain_task: asyncio.Task | None = None
        ticker_task: asyncio.Task | None = None
        try:
            # 1. 建立独立 LISTEN 连接
            listen_conn = await asyncpg.connect(self._dsn)
            await listen_conn.add_listener("context_changed", self._on_notify)

            # 2. 启动单条 drain loop + 周期唤醒 task
            drain_task = asyncio.create_task(self._drain_loop())
            ticker_task = asyncio.create_task(self._periodic_wakeup())
        except Exception:
            for task in (ticker_task, drain_task):
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            if listen_conn:
                await listen_conn.close()
            self._listen_conn = None
            self._drain_task = None
            self._ticker_task = None
            self._running = False
            raise

        self._listen_conn = listen_conn
        self._drain_task = drain_task
        self._ticker_task = ticker_task
        self._running = True

        # 3. startup drain
        self._wakeup.set()
        logger.info("PropagationEngine started")

    async def stop(self) -> None:
        """停止传播引擎。"""
        self._running = False
        self._wakeup.set()
        for task in (self._ticker_task, self._drain_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._ticker_task = None
        self._drain_task = None
        if self._listen_conn:
            await self._listen_conn.close()
            self._listen_conn = None
        self._priority_context_ids.clear()
        logger.info("PropagationEngine stopped")

    def _on_notify(self, conn, pid, channel, payload: str) -> None:
        """PG NOTIFY 回调：记录 priority context，然后唤醒唯一 drain loop。"""
        logger.debug("NOTIFY received: context_id=%s", payload)
        self._priority_context_ids.add(payload)
        self._wakeup.set()

    async def _periodic_wakeup(self) -> None:
        """周期唤醒唯一 drain loop，兜住漏通知和 crash 窗口。"""
        while self._running:
            try:
                await asyncio.sleep(self._sweep_interval)
                self._wakeup.set()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Periodic wakeup error")

    async def _requeue_stuck_events(self) -> None:
        """回收超过 lease_timeout 仍在 processing 的事件。"""
        async with self._pool.acquire() as conn:
            count = await conn.execute(
                """
                UPDATE change_events
                SET delivery_status = 'retry',
                    next_retry_at = NOW(),
                    claimed_at = NULL,
                    heartbeat_at = NULL,
                    lease_token = NULL,
                    lease_owner = NULL,
                    updated_at = NOW(),
                    last_error = COALESCE(last_error, 'processing lease expired')
                WHERE delivery_status IN ('leased', 'processing')
                  AND COALESCE(heartbeat_at, claimed_at) < NOW() - $1::interval
                """,
                timedelta(seconds=self._lease_timeout),
            )
            if count and count != "UPDATE 0":
                logger.info("Requeued stuck events: %s", count)
    async def _drain_loop(self) -> None:
        """唯一允许 claim 事件的后台循环。"""
        while self._running:
            try:
                await self._wakeup.wait()
                self._wakeup.clear()

                await self._requeue_stuck_events()

                # 先 drain NOTIFY 指向的 context backlog，再 drain 全局 backlog
                while self._priority_context_ids:
                    context_id = self._priority_context_ids.pop()
                    await self._drain_ready_events(context_id=context_id)

                await self._drain_ready_events(context_id=None)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Drain loop error")

    async def _drain_ready_events(self, context_id: str | None) -> None:
        while self._running:
            events = await self._claim_ready_events(context_id=context_id, limit=1)
            if not events:
                return
            await self._process_claimed_event(events[0])

    async def drain_once(
        self,
        *,
        context_id: str | None = None,
        limit: int = 100,
    ) -> DrainReport:
        """Process one durable batch and report every unfinished event."""
        await self._requeue_stuck_events()
        # A lease starts when claimed.  Claiming a large serial batch makes later
        # rows expire before processing, so one drain call owns at most one row.
        events = await self._claim_ready_events(
            context_id=context_id, limit=1 if limit else 0
        )
        errors: list[str] = []
        for event in events:
            try:
                await self._process_claimed_event(event)
            except Exception as exc:
                errors.append(f"{event.get('event_id')}: {type(exc).__name__}: {exc}")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT event_id, delivery_status
                  FROM change_events
                 WHERE ($1::uuid IS NULL OR context_id = $1::uuid)
                   AND delivery_status NOT IN ('processed', 'succeeded')
                 ORDER BY timestamp, event_id
                """,
                context_id,
            )
        statuses = [str(row["delivery_status"]) for row in rows]
        return DrainReport(
            claimed=len(events),
            succeeded=sum(
                1 for event in events
                if not any(str(row["event_id"]) == str(event["event_id"]) for row in rows)
            ),
            retryable=sum(s in ("pending", "retry", "leased", "processing") for s in statuses),
            failed=sum(s in ("failed", "dead_letter") for s in statuses),
            unfinished=len(rows),
            leftover_event_ids=tuple(str(row["event_id"]) for row in rows),
            errors=tuple(errors),
        )

    async def _claim_ready_events(
        self, context_id: str | None, limit: int
    ) -> list[dict]:
        """Atomically claim ready events with row locks and a stable lease token."""
        limit = min(max(0, limit), 1)
        async with self._pool.acquire() as conn:
            lease_token = uuid4()
            rows = await conn.fetch(
                """
                WITH ready AS (
                  SELECT event_id
                    FROM change_events
                   WHERE ($1::uuid IS NULL OR context_id = $1::uuid)
                     AND delivery_status IN ('pending', 'retry')
                     AND next_retry_at <= NOW()
                   ORDER BY timestamp ASC, event_id
                   FOR UPDATE SKIP LOCKED
                   LIMIT $2
                )
                UPDATE change_events e
                   SET delivery_status = 'processing',
                       claimed_at = NOW(),
                       heartbeat_at = NOW(),
                       lease_token = $3,
                       lease_owner = $4,
                       attempt_count = attempt_count + 1,
                       updated_at = NOW(),
                       last_error = NULL
                  FROM ready
                 WHERE e.event_id = ready.event_id
                RETURNING e.*
                """,
                context_id,
                limit,
                lease_token,
                self._worker_id,
            )
            return [dict(r) for r in rows]
    async def _process_claimed_event(self, event: dict) -> None:
        """处理一个已领取的事件。"""
        event_id = event["event_id"]
        change_type = event.get("change_type", "")
        lease_token = event.get("lease_token")

        if int(event.get("depth") or 0) > self._max_event_depth:
            await self._finish_event(
                event_id,
                success=False,
                terminal=True,
                error=f"maximum propagation depth {self._max_event_depth} exceeded",
                lease_token=lease_token,
            )
            return
        if event.get("root_event_id"):
            async with self._pool.acquire() as conn:
                root_count = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                      FROM change_events
                     WHERE root_event_id = $1 OR event_id = $1
                    """,
                    event["root_event_id"],
                )
            if int(root_count or 0) > self._max_events_per_root:
                await self._finish_event(
                    event_id,
                    success=False,
                    terminal=True,
                    error=(
                        f"maximum events per root {self._max_events_per_root} exceeded"
                    ),
                    lease_token=lease_token,
                )
                return
        await self._trace(
            event_id,
            "event_started",
            {
                "worker_id": self._worker_id,
                "attempt": int(event.get("attempt_count") or 0),
                "source_context_id": str(event.get("context_id")),
                "source_version": event.get("source_version") or event.get("new_version"),
                "parent_event_id": (
                    str(event["parent_event_id"]) if event.get("parent_event_id") else None
                ),
                "plan_id": str(event["plan_id"]) if event.get("plan_id") else None,
                "depth": int(event.get("depth") or 0),
            },
        )

        # deleted 事件不传播。marked_stale 默认也不传播；
        # 仅当 cascade_on_stale 开启时沿 derived_from 边级联。幂等键绑定具体
        # parent cause，因此 reconvergence 可让同一节点按不同路径收到多个事件。
        # runtime 只以 max_event_depth/max_events_per_root 作最后边界；调用方若要
        # 声明 frontier 完整，必须预先验证目标图是 DAG 且在冻结边界内。
        if change_type == "deleted":
            await self._finish_event(event_id, success=True, lease_token=lease_token)
            return
        if change_type == "marked_stale" and not self._cascade_on_stale:
            await self._finish_event(event_id, success=True, lease_token=lease_token)
            return

        all_succeeded = True

        # 路径 A：按 event.timestamp 查 dependencies（event-time 语义）
        try:
            dependents = await self._fetch_dependents(
                event["context_id"], event["timestamp"]
            )
        except Exception:
            logger.exception("Failed to fetch dependents for event %s", event_id)
            dependents = []
            all_succeeded = False

        for dep in dependents:
            effect_key = (
                f"dependency:{dep['dep_type']}:{dep['dependent_id']}:"
                f"{event.get('source_version') or event.get('new_version') or ''}"
            )
            try:
                await self._heartbeat(event)
                # 级联（marked_stale 放行）只作用于 derived_from 边，避免误触发
                # 其它 dep_type 的规则。
                if change_type == "marked_stale" and dep["dep_type"] != "derived_from":
                    continue
                if not await self._claim_effect(
                    event,
                    effect_key=effect_key,
                    effect_type="dependency",
                    target_context_id=dep["dependent_id"],
                ):
                    continue
                await self._record_risk_once(event, dep)
                rule = self._registry.get_dep_rule(dep["dep_type"])
                if rule is None:
                    logger.warning("No rule for dep_type=%s", dep["dep_type"])
                    await self._finish_effect(
                        event, effect_key, succeeded=True,
                        result={"action": "no_action", "reason": "missing_rule"},
                    )
                    continue
                action = await rule.evaluate(event, dep)
                effect_result = {
                    "edge": [str(event["context_id"]), str(dep["dependent_id"])],
                    "dep_type": dep["dep_type"],
                    "semantic_verdict": action.action,
                    "reason": action.reason,
                    "reason_hash": hashlib.sha256(
                        (action.reason or "").encode("utf-8")
                    ).hexdigest(),
                }
                finished_atomically = await self._execute_action(
                    action,
                    dep["dependent_id"],
                    event,
                    effect_key=effect_key,
                    effect_result=effect_result,
                )
                if not finished_atomically:
                    await self._finish_effect(
                        event,
                        effect_key,
                        succeeded=True,
                        result=effect_result,
                    )
            except LeaseLostError:
                logger.warning("Lease lost while processing event %s; stopping stale worker", event_id)
                return
            except Exception:
                logger.exception(
                    "Propagation failed for dependency %s of event %s",
                    dep["dependent_id"], event_id,
                )
                await self._finish_effect(
                    event,
                    effect_key,
                    succeeded=False,
                    result={"error": "dependency_effect_failed"},
                )
                all_succeeded = False

        # 路径 B：按 event.timestamp 查 skill_subscriptions（仅对 version_published 事件）
        if change_type == "version_published":
            try:
                subscribers = await self._fetch_subscribers(
                    event["context_id"], event["account_id"], event["timestamp"]
                )
            except Exception:
                logger.exception("Failed to fetch subscribers for event %s", event_id)
                subscribers = []
                all_succeeded = False

            for sub in subscribers:
                effect_key = (
                    f"subscription:{sub['agent_id']}:"
                    f"{event.get('source_version') or event.get('new_version') or ''}"
                )
                try:
                    await self._heartbeat(event)
                    if not await self._claim_effect(
                        event,
                        effect_key=effect_key,
                        effect_type="subscription",
                        target_context_id=None,
                    ):
                        continue
                    action = await self._registry.subscription_rule.evaluate(event, sub)
                    await self._execute_subscription_action(action, sub, event)
                    await self._finish_effect(
                        event, effect_key, succeeded=True,
                        result={"action": action.action},
                    )
                except LeaseLostError:
                    logger.warning(
                        "Lease lost while processing subscription event %s", event_id
                    )
                    return
                except Exception:
                    logger.exception(
                        "Notification failed for subscriber %s of event %s",
                        sub["agent_id"], event_id,
                    )
                    await self._finish_effect(
                        event, effect_key, succeeded=False,
                        result={"error": "subscription_effect_failed"},
                    )
                    all_succeeded = False

        await self._finish_event(
            event_id,
            success=all_succeeded,
            lease_token=lease_token,
            error=None if all_succeeded else "partial propagation failure",
        )

    async def _claim_effect(
        self,
        event: dict,
        *,
        effect_key: str,
        effect_type: str,
        target_context_id,
    ) -> bool:
        """Claim one effect only while this transaction still owns the event."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                owned = await conn.fetchval(
                    """
                    SELECT 1 FROM change_events
                     WHERE event_id = $1 AND lease_token = $2
                       AND delivery_status = 'processing'
                     FOR UPDATE
                    """,
                    event["event_id"],
                    event.get("lease_token"),
                )
                if owned is None:
                    raise LeaseLostError(f"event lease lost: {event['event_id']}")
                row = await conn.fetchrow(
                    """
                INSERT INTO propagation_effects (
                  event_id, effect_key, effect_type, target_context_id,
                  source_version, status, lease_token
                )
                VALUES ($1, $2, $3, $4, $5, 'started', $6)
                ON CONFLICT (event_id, effect_key) DO UPDATE
                  SET status = 'started',
                      lease_token = EXCLUDED.lease_token,
                      updated_at = NOW()
                WHERE propagation_effects.status = 'failed'
                   OR (
                     propagation_effects.status = 'started'
                     AND propagation_effects.updated_at < NOW() - $7::interval
                   )
                RETURNING status
                """,
                    event["event_id"],
                    effect_key,
                    effect_type,
                    target_context_id,
                    self._event_source_version(event),
                    event.get("lease_token"),
                    timedelta(seconds=self._lease_timeout),
                )
            return row is not None

    async def _heartbeat(self, event: dict) -> None:
        lease_token = event.get("lease_token")
        if lease_token is None:
            raise LeaseLostError(f"event has no lease token: {event['event_id']}")
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE change_events
                   SET heartbeat_at = NOW(), updated_at = NOW()
                 WHERE event_id = $1 AND lease_token = $2
                   AND delivery_status = 'processing'
                """,
                event["event_id"],
                lease_token,
            )
        if _affected_rows(result) != 1:
            raise LeaseLostError(f"event lease lost: {event['event_id']}")

    async def _record_risk_once(self, event: dict, dep: dict) -> None:
        """Deduct an executed edge's planned risk at most once."""
        edge_key = f"{event['context_id']}->{dep['dependent_id']}"
        metadata = event.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        planned = event.get("plan_id") is not None or bool(metadata.get("plan_required"))
        if not planned:
            return
        assignments = metadata.get("assignments")
        risk_by_edge = metadata.get("risk_delta_by_edge")
        if (
            event.get("plan_id") is None
            or not event.get("graph_scope")
            or not isinstance(assignments, dict)
            or edge_key not in assignments
            or not isinstance(risk_by_edge, dict)
            or edge_key not in risk_by_edge
        ):
            raise RuntimeError(f"incomplete planned risk metadata for executed edge {edge_key}")
        risk_delta = float(risk_by_edge[edge_key])
        if risk_delta < 0:
            raise RuntimeError(f"negative planned risk for edge {edge_key}")
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO propagation_risk_ledger (
                  event_id, edge_key, plan_id, source_version, risk_delta
                )
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (event_id, edge_key) DO NOTHING
                """,
                event["event_id"],
                edge_key,
                event.get("plan_id"),
                self._event_source_version(event),
                risk_delta,
            )
        await self._trace(
            event["event_id"],
            "planner_assignment",
            {
                "edge_key": edge_key,
                "plan_id": str(event["plan_id"]) if event.get("plan_id") else None,
                "source_version": self._event_source_version(event),
                "risk_delta": risk_delta,
                "assignment": (
                    assignments.get(edge_key)
                ),
            },
        )

    async def _finish_effect(
        self,
        event: dict,
        effect_key: str,
        *,
        succeeded: bool,
        result: dict,
    ) -> None:
        event_id = event["event_id"]
        lease_token = event.get("lease_token")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                result_tag = await conn.execute(
                    """
                UPDATE propagation_effects
                   SET status = $3,
                       result = $4::jsonb,
                       updated_at = NOW()
                 WHERE event_id = $1 AND effect_key = $2
                   AND lease_token = $5
                   AND EXISTS (
                     SELECT 1 FROM change_events e
                      WHERE e.event_id = $1 AND e.lease_token = $5
                        AND e.delivery_status = 'processing'
                   )
                """,
                    event_id,
                    effect_key,
                    "succeeded" if succeeded else "failed",
                    json.dumps(result, sort_keys=True),
                    lease_token,
                )
                if _affected_rows(result_tag) != 1:
                    raise LeaseLostError(f"effect lease lost: {event_id}/{effect_key}")
        await self._trace(
            event_id,
            "effect_succeeded" if succeeded else "effect_failed",
            {"effect_key": effect_key, **result},
        )

    @staticmethod
    async def _lock_effect_fence(
        db: ScopedRepo,
        event: dict,
        effect_key: str,
    ) -> None:
        """Lock and verify both leases before any persistent effect."""
        lease_token = event.get("lease_token")
        if lease_token is None:
            raise LeaseLostError(f"event has no lease token: {event['event_id']}")
        owned = await db.fetchval(
            """
            SELECT 1 FROM change_events e
              JOIN propagation_effects p ON p.event_id = e.event_id
             WHERE e.event_id = $1
               AND e.delivery_status = 'processing'
               AND e.lease_token = $2
               AND p.effect_key = $3
               AND p.status = 'started'
               AND p.lease_token = $2
             FOR UPDATE OF e, p
            """,
            event["event_id"],
            lease_token,
            effect_key,
        )
        if owned is None:
            raise LeaseLostError(
                f"event/effect lease lost: {event['event_id']}/{effect_key}"
            )

    @staticmethod
    async def _finish_effect_in_session(
        db: ScopedRepo,
        event: dict,
        effect_key: str,
        *,
        succeeded: bool,
        result: dict,
    ) -> None:
        """Commit effect state in the same transaction as its DB side effect."""
        result_tag = await db.execute(
            """
            UPDATE propagation_effects
               SET status = $3,
                   result = $4::jsonb,
                   updated_at = NOW()
             WHERE event_id = $1
               AND effect_key = $2
               AND status = 'started'
               AND lease_token = $5
            """,
            event["event_id"],
            effect_key,
            "succeeded" if succeeded else "failed",
            json.dumps(result, sort_keys=True),
            event.get("lease_token"),
        )
        if _affected_rows(result_tag) != 1:
            raise LeaseLostError(
                f"effect lease lost: {event['event_id']}/{effect_key}"
            )

    @staticmethod
    def _event_source_version(event: dict) -> int | None:
        value = event.get("source_version") or event.get("new_version")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    async def _fetch_dependents(self, context_id, event_ts) -> list[dict]:
        """查询事件发生时已经存在的依赖边。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT d.dependent_id, d.dep_type, d.pinned_version,
                       d.created_at, c.version AS target_version,
                       c.validity_status AS target_validity_status
                FROM dependencies d
                JOIN contexts c ON c.id = d.dependent_id
                WHERE d.dependency_id = $1
                  AND d.created_at <= $2
                ORDER BY created_at ASC
                """,
                context_id,
                event_ts,
            )
            return [dict(r) for r in rows]

    async def _fetch_subscribers(self, skill_id, account_id: str, event_ts) -> list[dict]:
        """查询事件发生时已经存在的订阅。需要租户上下文（RLS）。"""
        async with self._repo.session(account_id) as db:
            rows = await db.fetch(
                """
                SELECT agent_id, pinned_version, created_at
                FROM skill_subscriptions
                WHERE skill_id = $1
                  AND created_at <= $2
                ORDER BY created_at ASC
                """,
                skill_id,
                event_ts,
            )
            return [dict(r) for r in rows]
    async def _execute_action(
        self,
        action: PropagationAction,
        dependent_id,
        event: dict,
        *,
        effect_key: str,
        effect_result: dict,
    ) -> bool:
        """Execute an action; return whether effect success committed atomically."""
        if action.action == "no_action":
            return False

        if action.action == "mark_stale":
            await self._mark_stale(
                dependent_id,
                event,
                action.reason,
                effect_key=effect_key,
                effect_result=effect_result,
            )
            return True
        elif action.action in ("notify", "advisory"):
            logger.info(
                "Propagation %s for dependent %s: %s",
                action.action, dependent_id, action.reason,
            )
        return False

    async def _execute_subscription_action(
        self, action: PropagationAction, subscriber: dict, event: dict
    ) -> None:
        """执行一个订阅通知副作用。MVP 中仅日志。"""
        if action.action in ("notify", "advisory"):
            logger.info(
                "Subscription %s for agent %s: %s",
                action.action, subscriber["agent_id"], action.reason,
            )

    async def _mark_stale(
        self,
        dependent_id,
        event: dict,
        reason: str,
        *,
        effect_key: str,
        effect_result: dict,
    ) -> None:
        """Mark stale and finish its effect under one event/effect lease lock."""
        async with self._repo.session(event["account_id"]) as db:
            await self._lock_effect_fence(db, event, effect_key)
            await self._mark_stale_in_session(db, dependent_id, event, reason)
            await self._finish_effect_in_session(
                db,
                event,
                effect_key,
                succeeded=True,
                result=effect_result,
            )
        await self._trace(
            event["event_id"],
            "effect_succeeded",
            {"effect_key": effect_key, **effect_result},
        )

    async def _mark_stale_in_session(
        self,
        db: ScopedRepo,
        dependent_id,
        event: dict,
        reason: str,
    ) -> None:
        ctx = make_system_context(event["account_id"], "propagation_engine")
        try:
            if isinstance(self._lifecycle, LifecycleService):
                await self._lifecycle.mark_stale(
                    db,
                    dependent_id,
                    reason,
                    ctx=ctx,
                    source_event=event,
                )
            else:
                await self._lifecycle.mark_stale(
                    db, dependent_id, reason, ctx=ctx
                )
        except NotFoundError:
            logger.info(
                "Skip stale mark for missing/deleted dependent_id=%s reason=%s",
                dependent_id,
                reason,
            )
            return
        logger.info("Marked stale: dependent_id=%s reason=%s", dependent_id, reason)

    async def _trace(self, event_id, trace_type: str, payload: dict) -> None:
        """Persist replay-safe structured trace data (hashes, ids, no prompts/secrets)."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO propagation_trace (event_id, trace_type, payload)
                VALUES ($1, $2, $3::jsonb)
                """,
                event_id,
                trace_type,
                json.dumps(payload, sort_keys=True),
            )

    async def _finish_event(
        self,
        event_id,
        *,
        success: bool,
        terminal: bool = False,
        error: str | None = None,
        lease_token=None,
    ) -> None:
        """Finish, retry, or dead-letter an event while fencing stale workers."""
        async with self._pool.acquire() as conn:
            if success:
                result = await conn.execute(
                    """
                    UPDATE change_events
                    SET delivery_status = 'succeeded',
                        processed_at = NOW(),
                        claimed_at = NULL,
                        heartbeat_at = NULL,
                        lease_token = NULL,
                        lease_owner = NULL,
                        updated_at = NOW(),
                        last_error = NULL
                    WHERE event_id = $1
                      AND ($2::uuid IS NULL OR lease_token = $2)
                    """,
                    event_id,
                    lease_token,
                )
            else:
                result = await conn.execute(
                    """
                    UPDATE change_events
                    SET delivery_status = CASE
                          WHEN $3 OR attempt_count >= max_attempts
                            THEN 'dead_letter'
                          ELSE 'retry'
                        END,
                        claimed_at = NULL,
                        heartbeat_at = NULL,
                        lease_token = NULL,
                        lease_owner = NULL,
                        updated_at = NOW(),
                        next_retry_at = NOW() + make_interval(secs => LEAST(300, 5 * attempt_count)),
                        last_error = $4,
                        terminal_reason = CASE
                          WHEN $3 OR attempt_count >= max_attempts THEN $4
                          ELSE terminal_reason
                        END
                    WHERE event_id = $1
                      AND ($2::uuid IS NULL OR lease_token = $2)
                    """,
                    event_id,
                    lease_token,
                    terminal,
                    error or "partial propagation failure",
                )
        if lease_token is not None and _affected_rows(result) != 1:
            raise LeaseLostError(f"event lease lost before finish: {event_id}")
        await self._trace(
            event_id,
            "event_succeeded" if success else ("event_failed" if terminal else "event_retry"),
            {"error": error, "terminal": terminal},
        )
