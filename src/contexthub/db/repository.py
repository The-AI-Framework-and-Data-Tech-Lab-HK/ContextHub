from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any
import re

import asyncpg
import psycopg
from psycopg.rows import dict_row

from contexthub.config import Settings


_DOLLAR_PARAM_PATTERN = re.compile(r"\$([1-9][0-9]*)")


def _rewrite_dollar_params(sql: str, args: tuple[Any, ...]) -> tuple[str, tuple[Any, ...]]:
    """Convert $n placeholders to %s placeholders for psycopg."""
    indexes: list[int] = []

    def _replace(match: re.Match[str]) -> str:
        idx = int(match.group(1)) - 1
        indexes.append(idx)
        return "%s"

    rewritten = _DOLLAR_PARAM_PATTERN.sub(_replace, sql)
    if not indexes:
        return sql, args
    return rewritten, tuple(args[i] for i in indexes)


class _AsyncpgExecutor:
    def __init__(self, conn: asyncpg.Connection):
        self._conn = conn

    async def fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        return await self._conn.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> asyncpg.Record | None:
        return await self._conn.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await self._conn.fetchval(sql, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        return await self._conn.execute(sql, *args)


class _OpenGaussExecutor:
    def __init__(self, conn: psycopg.AsyncConnection[Any]):
        self._conn = conn

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        adapted_sql, adapted_args = _rewrite_dollar_params(sql, args)
        async with self._conn.cursor(row_factory=dict_row) as cur:
            if adapted_args:
                await cur.execute(adapted_sql, adapted_args)
            else:
                await cur.execute(adapted_sql)
            return await cur.fetchall()

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        adapted_sql, adapted_args = _rewrite_dollar_params(sql, args)
        async with self._conn.cursor(row_factory=dict_row) as cur:
            if adapted_args:
                await cur.execute(adapted_sql, adapted_args)
            else:
                await cur.execute(adapted_sql)
            row = await cur.fetchone()
            return row

    async def fetchval(self, sql: str, *args: Any) -> Any:
        adapted_sql, adapted_args = _rewrite_dollar_params(sql, args)
        async with self._conn.cursor() as cur:
            if adapted_args:
                await cur.execute(adapted_sql, adapted_args)
            else:
                await cur.execute(adapted_sql)
            row = await cur.fetchone()
            return row[0] if row is not None else None

    async def execute(self, sql: str, *args: Any) -> str:
        adapted_sql, adapted_args = _rewrite_dollar_params(sql, args)
        async with self._conn.cursor() as cur:
            if adapted_args:
                await cur.execute(adapted_sql, adapted_args)
            else:
                await cur.execute(adapted_sql)
            return cur.statusmessage or ""


class ScopedRepo:
    """Request-scoped 数据库执行器。所有 SQL 都必须通过它执行。"""

    def __init__(self, conn: _AsyncpgExecutor | _OpenGaussExecutor):
        self._conn = conn

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return await self._conn.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return await self._conn.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await self._conn.fetchval(sql, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        return await self._conn.execute(sql, *args)


class PgRepository:
    def __init__(self, pool: asyncpg.Pool | None, settings: Settings | None = None):
        self._pool = pool
        self._settings = settings or Settings()

    @asynccontextmanager
    async def session(self, account_id: str) -> AsyncIterator[ScopedRepo]:
        if self._settings.is_opengauss:
            conn = await psycopg.AsyncConnection.connect(self._settings.asyncpg_database_url)
            try:
                await conn.execute("BEGIN")
                scoped_repo = ScopedRepo(_OpenGaussExecutor(conn))
                await scoped_repo.execute(
                    "SELECT set_config('app.account_id', $1, true)",
                    account_id,
                )
                try:
                    yield scoped_repo
                except Exception:
                    await conn.rollback()
                    raise
                else:
                    await conn.commit()
            finally:
                await conn.close()
            return

        if self._pool is None:
            raise RuntimeError("Postgres backend requires an asyncpg pool")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # asyncpg does not support bind parameters in SET statements.
                await conn.execute(
                    "SELECT set_config('app.account_id', $1, true)",
                    account_id,
                )
                yield ScopedRepo(_AsyncpgExecutor(conn))
