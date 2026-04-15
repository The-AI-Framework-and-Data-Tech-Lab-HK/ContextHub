"""Periodic lifecycle sweep across tenants."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging

import asyncpg

from contexthub.db.repository import PgRepository
from contexthub.errors import NotFoundError
from contexthub.services.lifecycle_service import LifecycleService, make_system_context

logger = logging.getLogger(__name__)


class LifecycleScheduler:
    def __init__(
        self,
        lifecycle: LifecycleService,
        repo: PgRepository,
        pool: asyncpg.Pool,
        interval_seconds: int = 3600,
    ):
        self._lifecycle = lifecycle
        self._repo = repo
        self._pool = pool
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def run_once(self) -> None:
        await self._sweep()

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Lifecycle sweep failed")

            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    async def _sweep(self) -> None:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT account_id
                FROM change_events
                ORDER BY account_id
                """
            )

        for row in rows:
            try:
                await self._sweep_tenant(row["account_id"])
            except Exception:
                logger.exception("Lifecycle sweep failed for tenant %s", row["account_id"])

    async def _sweep_tenant(self, account_id: str) -> None:
        ctx = make_system_context(account_id, "lifecycle_scheduler")
        async with self._repo.session(account_id) as db:
            await self._lifecycle.ensure_default_policies(db, ctx=ctx)

            stale_candidates = await db.fetch(
                """
                SELECT c.id
                FROM contexts c
                JOIN lifecycle_policies lp
                  ON c.context_type = lp.context_type
                 AND c.scope = lp.scope
                WHERE c.status = 'active'
                  AND lp.stale_after_days > 0
                  AND c.last_accessed_at < NOW() - (lp.stale_after_days || ' days')::interval
                """
            )
            for row in stale_candidates:
                try:
                    await self._lifecycle.mark_stale(
                        db, row["id"], "auto_stale_policy", ctx=ctx
                    )
                except NotFoundError:
                    logger.info(
                        "Skip missing context during stale sweep: tenant=%s context_id=%s",
                        account_id,
                        row["id"],
                    )

            archive_candidates = await db.fetch(
                """
                SELECT c.id
                FROM contexts c
                JOIN lifecycle_policies lp
                  ON c.context_type = lp.context_type
                 AND c.scope = lp.scope
                WHERE c.status = 'stale'
                  AND lp.archive_after_days > 0
                  AND c.stale_at < NOW() - (lp.archive_after_days || ' days')::interval
                """
            )
            for row in archive_candidates:
                try:
                    await self._lifecycle.mark_archived(db, row["id"], ctx=ctx)
                except NotFoundError:
                    logger.info(
                        "Skip missing context during archive sweep: tenant=%s context_id=%s",
                        account_id,
                        row["id"],
                    )

            delete_candidates = await db.fetch(
                """
                SELECT c.id
                FROM contexts c
                JOIN lifecycle_policies lp
                  ON c.context_type = lp.context_type
                 AND c.scope = lp.scope
                WHERE c.status = 'archived'
                  AND lp.delete_after_days > 0
                  AND c.archived_at < NOW() - (lp.delete_after_days || ' days')::interval
                """
            )
            for row in delete_candidates:
                try:
                    await self._lifecycle.mark_deleted(db, row["id"], ctx=ctx)
                except NotFoundError:
                    logger.info(
                        "Skip missing context during delete sweep: tenant=%s context_id=%s",
                        account_id,
                        row["id"],
                    )
