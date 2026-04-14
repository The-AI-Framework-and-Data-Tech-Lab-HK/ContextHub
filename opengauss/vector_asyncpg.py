#!/usr/bin/env python3
"""Minimal repro for asyncpg + openGauss vector result decoding.

What this script demonstrates:
1. Queries that do NOT return the vector column succeed.
2. Queries that DO return the vector column fail in asyncpg on openGauss.

Default DSN matches the repo's openGauss setup guide. Override with:
  VECTOR_REPRO_DSN=postgresql://user:pass@host:port/db python scripts/repro_asyncpg_opengauss_vector.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg


DEFAULT_DSN = "postgresql://contexthub:ContextHub%40123@localhost:15432/contexthub"
TEMP_TABLE = "tmp_vector_repro"


async def _run_query(conn: asyncpg.Connection, label: str, sql: str, method: str) -> None:
    print(f"\n[{label}]")
    print(f"SQL: {sql}")
    try:
        result = await getattr(conn, method)(sql)
        print("status: OK")
        print(f"result: {result}")
    except Exception as exc:
        print("status: ERROR")
        print(f"type: {type(exc).__name__}")
        print(f"message: {exc}")


async def main() -> int:
    dsn = os.environ.get("VECTOR_REPRO_DSN", DEFAULT_DSN)

    print("Connecting with asyncpg...")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"DROP TABLE IF EXISTS {TEMP_TABLE}")
        await conn.execute(
            f"""
            CREATE TABLE {TEMP_TABLE} (
                id   INT PRIMARY KEY,
                note TEXT,
                emb  vector(3)
            )
            """
        )
        await conn.execute(
            f"""
            INSERT INTO {TEMP_TABLE} (id, note, emb)
            VALUES (1, 'vector row', '[1,2,3]')
            """
        )

        print("Inserted one row with a vector value.")

        await _run_query(
            conn,
            "Control case: query without vector column",
            f"SELECT id, note FROM {TEMP_TABLE}",
            "fetch",
        )
        await _run_query(
            conn,
            "Failure case: query only the vector column",
            f"SELECT emb FROM {TEMP_TABLE}",
            "fetch",
        )
        await _run_query(
            conn,
            "Failure case: SELECT * includes the vector column",
            f"SELECT * FROM {TEMP_TABLE}",
            "fetch",
        )
        await _run_query(
            conn,
            "Failure case: fetchrow also fails when result contains vector",
            f"SELECT * FROM {TEMP_TABLE}",
            "fetchrow",
        )
    finally:
        await conn.execute(f"DROP TABLE IF EXISTS {TEMP_TABLE}")
        await conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
