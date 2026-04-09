"""AuditService: tiered audit logging.

Tier 1 (log_strict): fail-closed — audit INSERT in the business transaction.
    Audit failure = business rollback.
    For: create, update, delete, write, promote, publish, policy_change.

Tier 2 (log_best_effort): best-effort — audit INSERT wrapped in SAVEPOINT.
    Audit failure does NOT block the business operation.
    For: read, search, ls, stat.

access_denied (log_access_denied): independent connection — 独立于业务事务。
    设计文档将 access_denied 归为 Tier 1，但其实现偏离了 fail-closed 语义。
    原因：ACL deny 后会 raise ForbiddenError，导致主事务 ROLLBACK，
    如果审计 INSERT 在主事务内则会随之丢失。因此使用 pool.acquire(timeout)
    获取独立连接来持久化审计记录。timeout 防止在高并发 deny 场景下因
    连接池耗尽而死锁（请求已持有一条连接，若 acquire 无限等待则谁也
    释放不了）。如果获取连接超时或写入失败，降级为 warning 日志，
    deny 行为（ForbiddenError）仍然正常执行。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from contexthub.db.repository import ScopedRepo

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

_AUDIT_CONN_TIMEOUT = 1.0


class AuditService:

    def __init__(self, pool: asyncpg.Pool | None = None):
        self._pool = pool

    async def log_strict(
        self,
        db: ScopedRepo,
        actor: str,
        action: str,
        resource_uri: str | None,
        result: str,
        context_used: list[str] | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Tier 1: fail-closed。审计 INSERT 在业务事务内，失败则整个事务回滚。"""
        await db.execute(
            """
            INSERT INTO audit_log
                (actor, action, resource_uri, context_used, result, metadata, account_id)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, current_setting('app.account_id'))
            """,
            actor,
            action,
            resource_uri,
            context_used,
            result,
            json.dumps(metadata) if metadata else None,
        )

    async def log_best_effort(
        self,
        db: ScopedRepo,
        actor: str,
        action: str,
        resource_uri: str | None,
        result: str,
        context_used: list[str] | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Tier 2: best-effort。用 SAVEPOINT 包裹，失败时回滚审计但不阻塞业务。"""
        try:
            await db.execute("SAVEPOINT audit_sp")
            await db.execute(
                """
                INSERT INTO audit_log
                    (actor, action, resource_uri, context_used, result, metadata, account_id)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, current_setting('app.account_id'))
                """,
                actor,
                action,
                resource_uri,
                context_used,
                result,
                json.dumps(metadata) if metadata else None,
            )
            await db.execute("RELEASE SAVEPOINT audit_sp")
        except Exception:
            try:
                await db.execute("ROLLBACK TO SAVEPOINT audit_sp")
            except Exception:
                pass
            logger.warning(
                "Audit log failed for %s on %s", action, resource_uri, exc_info=True
            )

    async def log_access_denied(
        self,
        account_id: str,
        actor: str,
        resource_uri: str,
        metadata: dict | None = None,
    ) -> None:
        """access_denied 专用：使用独立连接，不受主事务 ROLLBACK 影响。

        通过 pool.acquire(timeout=_AUDIT_CONN_TIMEOUT) 获取连接，避免在
        高并发 deny 场景下因连接池耗尽而死锁：请求已通过 get_db() 持有
        一条连接，若 acquire 无限等待则所有请求互相阻塞。
        """
        if self._pool is None:
            logger.warning(
                "AuditService: no pool configured, skipping access_denied audit for %s",
                resource_uri,
            )
            return
        try:
            async with self._pool.acquire(timeout=_AUDIT_CONN_TIMEOUT) as conn:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT set_config('app.account_id', $1, true)",
                        account_id,
                    )
                    await conn.execute(
                        """
                        INSERT INTO audit_log
                            (actor, action, resource_uri, result, metadata, account_id)
                        VALUES ($1, 'access_denied', $2, 'denied', $3::jsonb, $4)
                        """,
                        actor,
                        resource_uri,
                        json.dumps(metadata) if metadata else None,
                        account_id,
                    )
        except Exception:
            logger.warning(
                "Audit log failed for access_denied on %s",
                resource_uri,
                exc_info=True,
            )
