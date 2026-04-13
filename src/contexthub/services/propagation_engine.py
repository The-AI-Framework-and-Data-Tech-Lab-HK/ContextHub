"""PropagationEngine: outbox consumer, retry/sweep scheduler."""

import asyncio
from contextlib import suppress
import logging
from datetime import timedelta

import asyncpg

from contexthub.db.repository import PgRepository, ScopedRepo
from contexthub.propagation.base import PropagationAction
from contexthub.propagation.registry import PropagationRuleRegistry
from contexthub.services.indexer_service import IndexerService

logger = logging.getLogger(__name__)


class PropagationEngine:
    """单实例 MVP：change_events 是 source of truth，NOTIFY 只负责唤醒。

    三个入口共用同一条串行 drain 逻辑：
    - start()            → 启动后台 drain loop，并立刻唤醒一次 startup drain
    - _on_notify()       → 记录待优先处理的 context_id，并唤醒 drain loop
    - _periodic_wakeup() → 周期唤醒 drain loop，兜住漏通知和 crash 窗口

    MVP 限制：单实例部署。当前实例内也只允许一个 claimer。
    如未来引入多实例或多 worker，再把 claim SQL 升级为 SELECT ... FOR UPDATE SKIP LOCKED。
    """

    def __init__(
        self,
        repo: PgRepository,
        pool: asyncpg.Pool,
        dsn: str,
        rule_registry: PropagationRuleRegistry,
        indexer: IndexerService,
        sweep_interval: int = 30,
        lease_timeout: int = 300,
    ):
        self._repo = repo
        self._pool = pool
        self._dsn = dsn
        self._registry = rule_registry
        self._indexer = indexer
        self._sweep_interval = sweep_interval
        self._lease_timeout = lease_timeout
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
            try:
                await listen_conn.add_listener("context_changed", self._on_notify)
            except asyncpg.exceptions.FeatureNotSupportedError:
                logger.warning(
                    "LISTEN/NOTIFY is not supported by the current database; "
                    "falling back to periodic propagation polling only."
                )
                await listen_conn.close()
                listen_conn = None

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
                    last_error = COALESCE(last_error, 'processing lease expired')
                WHERE delivery_status = 'processing'
                  AND claimed_at < NOW() - $1::interval
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
            events = await self._claim_ready_events(context_id=context_id, limit=100)
            if not events:
                return
            for event in events:
                await self._process_claimed_event(event)

    async def _claim_ready_events(
        self, context_id: str | None, limit: int
    ) -> list[dict]:
        """领取 ready 事件：pending/retry → processing。

        当前实现依赖"单实例 + 单 claimer"保证不会重复 claim。
        如果未来允许多个 claimers，这里必须改为 FOR UPDATE SKIP LOCKED。
        """
        async with self._pool.acquire() as conn:
            if context_id:
                rows = await conn.fetch(
                    """
                    UPDATE change_events
                    SET delivery_status = 'processing',
                        claimed_at = NOW(),
                        attempt_count = attempt_count + 1,
                        last_error = NULL
                    WHERE event_id IN (
                        SELECT event_id
                        FROM change_events
                        WHERE context_id = $1::uuid
                          AND delivery_status IN ('pending', 'retry')
                          AND next_retry_at <= NOW()
                        ORDER BY timestamp ASC
                        LIMIT $2
                    )
                    RETURNING *
                    """,
                    context_id,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    UPDATE change_events
                    SET delivery_status = 'processing',
                        claimed_at = NOW(),
                        attempt_count = attempt_count + 1,
                        last_error = NULL
                    WHERE event_id IN (
                        SELECT event_id
                        FROM change_events
                        WHERE delivery_status IN ('pending', 'retry')
                          AND next_retry_at <= NOW()
                        ORDER BY timestamp ASC
                        LIMIT $1
                    )
                    RETURNING *
                    """,
                    limit,
                )
            return [dict(r) for r in rows]
    async def _process_claimed_event(self, event: dict) -> None:
        """处理一个已领取的事件。"""
        event_id = event["event_id"]
        change_type = event.get("change_type", "")

        # 不对 marked_stale / deleted 事件做传播（防止循环）
        if change_type in ("marked_stale", "deleted"):
            await self._finish_event(event_id, success=True)
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
            try:
                rule = self._registry.get_dep_rule(dep["dep_type"])
                if rule is None:
                    logger.warning("No rule for dep_type=%s", dep["dep_type"])
                    continue
                action = await rule.evaluate(event, dep)
                await self._execute_action(action, dep["dependent_id"], event)
            except Exception:
                logger.exception(
                    "Propagation failed for dependency %s of event %s",
                    dep["dependent_id"], event_id,
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
                try:
                    action = await self._registry.subscription_rule.evaluate(event, sub)
                    await self._execute_subscription_action(action, sub, event)
                except Exception:
                    logger.exception(
                        "Notification failed for subscriber %s of event %s",
                        sub["agent_id"], event_id,
                    )
                    all_succeeded = False

        await self._finish_event(event_id, success=all_succeeded)
    async def _fetch_dependents(self, context_id, event_ts) -> list[dict]:
        """查询事件发生时已经存在的依赖边。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT dependent_id, dep_type, pinned_version, created_at
                FROM dependencies
                WHERE dependency_id = $1
                  AND created_at <= $2
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
        self, action: PropagationAction, dependent_id, event: dict
    ) -> None:
        """执行一个传播副作用。"""
        if action.action == "no_action":
            return

        if action.action == "mark_stale":
            await self._mark_stale(dependent_id, event, action.reason)
        elif action.action == "auto_update":
            await self._auto_update(dependent_id, event)
        elif action.action in ("notify", "advisory"):
            logger.info(
                "Propagation %s for dependent %s: %s",
                action.action, dependent_id, action.reason,
            )

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
        self, dependent_id, event: dict, reason: str
    ) -> None:
        """标记 dependent context 为 stale。幂等：已经 stale/archived/deleted 的不重复标记。"""
        async with self._repo.session(event["account_id"]) as db:
            await self._mark_stale_in_session(db, dependent_id, event, reason)

    async def _mark_stale_in_session(
        self,
        db: ScopedRepo,
        dependent_id,
        event: dict,
        reason: str,
    ) -> None:
        result = await db.execute(
            """
            UPDATE contexts
            SET status = 'stale', stale_at = NOW(), updated_at = NOW()
            WHERE id = $1
              AND status NOT IN ('stale', 'archived', 'deleted')
            """,
            dependent_id,
        )
        if result and result != "UPDATE 0":
            await db.execute(
                """
                INSERT INTO change_events
                    (context_id, account_id, change_type, actor, diff_summary)
                VALUES ($1, $2, 'marked_stale', 'propagation_engine', $3)
                """,
                dependent_id,
                event["account_id"],
                reason,
            )
            logger.info("Marked stale: dependent_id=%s reason=%s", dependent_id, reason)

    async def _auto_update(self, dependent_id, event: dict) -> None:
        """source-aware 刷新 dependent context 的派生投影（仅 L0/L1）。"""
        async with self._repo.session(event["account_id"]) as db:
            source = await db.fetchrow(
                """
                SELECT id, context_type, l0_content, l1_content, l2_content
                FROM contexts
                WHERE id = $1
                """,
                event["context_id"],
            )
            dependent = await db.fetchrow(
                """
                SELECT id, context_type, l2_content
                FROM contexts
                WHERE id = $1
                """,
                dependent_id,
            )

            if source is None or dependent is None or not dependent["l2_content"]:
                await self._mark_stale_in_session(
                    db,
                    dependent_id,
                    event,
                    "table_schema auto_update prerequisites missing; downgrade to stale",
                )
                return

            source_snapshot = (
                source["l2_content"] or source["l1_content"] or source["l0_content"] or ""
            )
            if not source_snapshot:
                await self._mark_stale_in_session(
                    db,
                    dependent_id,
                    event,
                    "table_schema source snapshot missing; downgrade to stale",
                )
                return

            regeneration_input = (
                dependent["l2_content"]
                + "\n\n[Upstream dependency update]\n"
                + source_snapshot
            )
            generated = await self._indexer.generate(
                dependent["context_type"],
                regeneration_input,
                metadata={
                    "propagation_source_context_id": str(source["id"]),
                    "propagation_source_change_type": event["change_type"],
                    "propagation_source_diff_summary": event.get("diff_summary"),
                },
            )
            await db.execute(
                """
                UPDATE contexts
                SET l0_content = $1, l1_content = $2, updated_at = NOW()
                WHERE id = $3
                """,
                generated.l0, generated.l1, dependent_id,
            )
            if generated.l0:
                updated = await self._indexer.update_embedding(
                    db,
                    dependent_id,
                    generated.l0,
                )
                if not updated:
                    raise RuntimeError(
                        f"Failed to update embedding for dependent_id={dependent_id}"
                    )
            else:
                await db.execute(
                    "UPDATE contexts SET l0_embedding = NULL WHERE id = $1",
                    dependent_id,
                )

            logger.info(
                "Auto-updated derived projections for dependent_id=%s using source_context_id=%s",
                dependent_id,
                source["id"],
            )

    async def _finish_event(self, event_id, *, success: bool) -> None:
        """将事件标记为 processed 或 retry。"""
        async with self._pool.acquire() as conn:
            if success:
                await conn.execute(
                    """
                    UPDATE change_events
                    SET delivery_status = 'processed',
                        processed_at = NOW(),
                        claimed_at = NULL,
                        last_error = NULL
                    WHERE event_id = $1
                    """,
                    event_id,
                )
            else:
                await conn.execute(
                    """
                    UPDATE change_events
                    SET delivery_status = 'retry',
                        claimed_at = NULL,
                        next_retry_at = NOW() + make_interval(secs => LEAST(300, 5 * attempt_count)),
                        last_error = 'partial propagation failure'
                    WHERE event_id = $1
                    """,
                    event_id,
                )
