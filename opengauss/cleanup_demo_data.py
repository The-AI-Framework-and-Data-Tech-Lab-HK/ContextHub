#!/usr/bin/env python3
"""Clean data possibly inserted by demo_e2e_opengauss.py.

Usage:
  python clean_for_opengauss.py

Environment:
  DEMO_DB_DSN  Optional asyncpg DSN. Defaults to local openGauss DSN used by demo.
  DEMO_ACCOUNT Optional account id. Defaults to "acme".
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Iterable
from uuid import UUID

import asyncpg


DEFAULT_DSN = "postgresql://contexthub:ContextHub%40123@localhost:15432/contexthub"
DEFAULT_ACCOUNT = "acme"

DEMO_SKILL_URI = "ctx://team/engineering/skills/sql-generator"
DEMO_MEMORY_CONTENT = (
    "The orders table uses user_id as FK to users. "
    "Always JOIN on orders.user_id = users.id."
)


def _format_count(tag: str, count: str) -> str:
    return f"  - {tag}: {count}"


async def _collect_target_contexts(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT id, uri
        FROM contexts
        WHERE account_id = current_setting('app.account_id')
          AND (
              uri = $1
              OR (
                  uri LIKE 'ctx://agent/query-agent/memories/mem-%'
                  AND l2_content = $2
              )
              OR (
                  uri LIKE 'ctx://team/engineering/memories/shared_knowledge/mem-%'
                  AND l2_content = $2
              )
              OR uri LIKE 'ctx://datalake/mock/%'
          )
        ORDER BY uri
        """,
        DEMO_SKILL_URI,
        DEMO_MEMORY_CONTENT,
    )


async def _delete_by_context_ids(conn: asyncpg.Connection, context_ids: Iterable[UUID]) -> None:
    ids = list(context_ids)
    if not ids:
        print("  - No matching contexts found, nothing to delete.")
        return

    print(_format_count("target contexts", str(len(ids))))

    statements: list[tuple[str, str]] = [
        (
            "query_templates",
            "DELETE FROM query_templates WHERE context_id = ANY($1::uuid[])",
        ),
        (
            "table_relationships",
            "DELETE FROM table_relationships WHERE table_id_a = ANY($1::uuid[]) OR table_id_b = ANY($1::uuid[])",
        ),
        (
            "lineage",
            "DELETE FROM lineage WHERE upstream_id = ANY($1::uuid[]) OR downstream_id = ANY($1::uuid[])",
        ),
        (
            "table_metadata",
            "DELETE FROM table_metadata WHERE context_id = ANY($1::uuid[])",
        ),
        (
            "skill_subscriptions",
            "DELETE FROM skill_subscriptions WHERE skill_id = ANY($1::uuid[])",
        ),
        (
            "skill_versions",
            "DELETE FROM skill_versions WHERE skill_id = ANY($1::uuid[])",
        ),
        (
            "dependencies",
            "DELETE FROM dependencies WHERE dependent_id = ANY($1::uuid[]) OR dependency_id = ANY($1::uuid[])",
        ),
        (
            "change_events",
            "DELETE FROM change_events WHERE context_id = ANY($1::uuid[])",
        ),
        (
            "contexts",
            "DELETE FROM contexts WHERE id = ANY($1::uuid[])",
        ),
    ]

    for label, sql in statements:
        result = await conn.execute(sql, ids)
        print(_format_count(label, result))


async def main() -> None:
    dsn = os.environ.get("DEMO_DB_DSN", DEFAULT_DSN)
    account = os.environ.get("DEMO_ACCOUNT", DEFAULT_ACCOUNT)

    print("Cleaning demo data for openGauss...")
    print(f"  - account: {account}")
    print(f"  - dsn: {dsn}")

    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.account_id', $1, true)", account)

            targets = await _collect_target_contexts(conn)
            for row in targets:
                print(f"  - match: {row['uri']}")

            await _delete_by_context_ids(conn, [row["id"] for row in targets])
    finally:
        await conn.close()

    print("Cleanup complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # pragma: no cover
        print(f"Cleanup failed: {exc}", file=sys.stderr)
        raise
