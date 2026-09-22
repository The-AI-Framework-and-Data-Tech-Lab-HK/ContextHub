"""Fail-open side-channel audit for EntCollabBench online enforcement.

The proxy must not write ``audit_log`` in the synchronous decision path: a slow
or broken audit write would look like an enforcement timeout. This module keeps
that write on a best-effort worker and logs failures instead of gating actions.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import json
import logging
import queue
import threading
from typing import Any

import asyncpg

from contexthub.config import Settings
from contexthub.db.codecs import init_pg_connection

logger = logging.getLogger(__name__)

AuditWriter = Callable[[dict[str, Any]], Awaitable[None]]


class ThreadedEnforcementAuditSink:
    """Queue decision records and persist them away from the proxy hot path."""

    def __init__(
        self,
        writer: AuditWriter,
        *,
        max_queue_size: int = 10_000,
        name: str = "entcollab-enforcement-audit",
    ):
        self._writer = writer
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=max_queue_size)
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def __call__(self, record: Any) -> None:
        try:
            payload = record.to_json() if hasattr(record, "to_json") else dict(record)
            self._queue.put_nowait(payload)
        except Exception:
            logger.warning("Failed to enqueue enforcement audit record", exc_info=True)

    def close(self, *, drain: bool = True, timeout: float = 5.0) -> None:
        if drain:
            self._queue.join()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            logger.warning("Enforcement audit queue full while closing; dropping shutdown signal")
            return
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                try:
                    asyncio.run(self._writer(item))
                except Exception:
                    logger.warning("Enforcement audit write failed", exc_info=True)
            finally:
                self._queue.task_done()


def build_pg_enforcement_audit_sink(
    *,
    dsn: str | None = None,
    account_id: str = "entcollab-runtime",
    run_id: str,
    max_queue_size: int = 10_000,
) -> ThreadedEnforcementAuditSink:
    """Build a fail-open audit sink that writes directly to Postgres."""

    database_url = dsn or Settings().asyncpg_database_url

    async def write(record: dict[str, Any]) -> None:
        await _write_pg_audit_record(database_url, account_id=account_id, run_id=run_id, record=record)

    return ThreadedEnforcementAuditSink(write, max_queue_size=max_queue_size)


async def _write_pg_audit_record(
    dsn: str,
    *,
    account_id: str,
    run_id: str,
    record: Mapping[str, Any],
) -> None:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await init_pg_connection(conn)
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.account_id', $1, true)", account_id)
            await conn.execute(
                """
                INSERT INTO audit_log
                    (actor, action, resource_uri, context_used, result, metadata, account_id)
                VALUES ($1, 'enforcement', $2, $3, 'success', $4::jsonb, $5)
                """,
                str(record.get("agent_id") or ""),
                _resource_uri(record),
                _context_used(record),
                json.dumps(_metadata(record, run_id=run_id), sort_keys=True),
                account_id,
            )
    finally:
        await conn.close()


def _resource_uri(record: Mapping[str, Any]) -> str:
    if record.get("boundary") == "handoff" or record.get("recipient"):
        return f"entcollab://handoff/{record.get('recipient') or 'unknown'}"
    return f"entcollab://tool/{record.get('server') or 'unknown'}/{record.get('tool_name') or 'unknown'}"


def _context_used(record: Mapping[str, Any]) -> list[str]:
    uris: list[str] = []
    for violation in record.get("violations") or []:
        if not isinstance(violation, Mapping):
            continue
        evidence = violation.get("evidence")
        if isinstance(evidence, Mapping) and evidence.get("uri"):
            uris.append(str(evidence["uri"]))
    return sorted(set(uris))


def _metadata(record: Mapping[str, Any], *, run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "boundary": "handoff" if record.get("recipient") else "tool_call",
        "mode": record.get("mode"),
        "verdict": record.get("verdict"),
        "action": record.get("action"),
        "allow": record.get("allow"),
        "would_block": record.get("would_block"),
        "would_repair": record.get("would_repair"),
        "forwarded": record.get("forwarded"),
        "patched": record.get("patched", False),
        "schema_source": record.get("schema_source"),
        "request_id": record.get("request_id"),
        "target": {
            "server": record.get("server"),
            "tool_name": record.get("tool_name"),
            "recipient": record.get("recipient"),
        },
        "guardrail_reason": record.get("reason"),
        "violations": record.get("violations") or [],
        "error": record.get("error"),
    }
