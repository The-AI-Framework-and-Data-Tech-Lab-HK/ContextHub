"""asyncpg JSON/JSONB codec registration.

asyncpg returns jsonb columns as raw ``str`` by default. Register these
codecs on every new connection so that jsonb values are transparently
decoded to Python dicts/lists and encoded from Python objects.

The encoder is backward-compatible: if a caller already passes a
pre-serialised JSON string (e.g. frozen services that call
``json.dumps()`` before handing the value to asyncpg), the string is
forwarded as-is.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg


def _encode_json(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


async def init_pg_connection(conn: asyncpg.Connection) -> None:
    """Intended as the ``init`` callback for :func:`asyncpg.create_pool`."""
    await conn.set_type_codec(
        "jsonb",
        encoder=_encode_json,
        decoder=json.loads,
        schema="pg_catalog",
        format="text",
    )
    await conn.set_type_codec(
        "json",
        encoder=_encode_json,
        decoder=json.loads,
        schema="pg_catalog",
        format="text",
    )
