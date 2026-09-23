"""Synthetic S2 fixtures; no model endpoints or production DB access."""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from contexthub.llm.retry_policy import RetryPolicy
from contexthub.models.execution import CallIdentity
from contexthub.models.knowledge import canonical_hash
from contexthub.services.execution_ledger import validate_append

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
HASH = canonical_hash({"synthetic": True})


def identity(**kwargs):
    return CallIdentity(
        account_id="s2-a",
        execution_id=uuid4(),
        call_id=uuid4(),
        strategy="fixture",
        stage="fixture",
        operation="generate",
        model_role="generator",
        code_version="fixture-v1",
        config_sha256=HASH,
        **kwargs,
    )


def policy(**kwargs):
    params = dict(
        max_attempts=3,
        timeout_seconds=1,
        backoff_seconds=(0, 0),
        retry_status_codes=(429, 500, 502, 503, 504),
        retry_transport_errors=("ReadTimeout", "ConnectError"),
    )
    params.update(kwargs)
    return RetryPolicy(**params)


class MemorySink:
    def __init__(self):
        self.rows = []
        self.lock = asyncio.Lock()

    async def append(self, record):
        async with self.lock:
            previous = [
                r for r in self.rows if r.identity.call_id == record.identity.call_id
            ]
            if validate_append(previous, record):
                self.rows.append(record)
