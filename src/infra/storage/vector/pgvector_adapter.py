"""PostgreSQL + pgvector adapter for trajectory vector storage."""

from __future__ import annotations

import json
from typing import Any

from psycopg import connect
from psycopg.rows import dict_row
from psycopg.sql import Identifier, SQL

from infra.storage.vector.base import VectorStoreAdapter


def _to_vector_literal(embedding: list[float]) -> str:
    # pgvector textual format: [1,2,3]
    return "[" + ",".join(str(float(x)) for x in embedding) + "]"


class PgVectorAdapter(VectorStoreAdapter):
    """Persist vectors and metadata in PostgreSQL with pgvector."""

    def __init__(self, *, dsn: str, table: str = "amc_trajectory_index", schema: str = "public") -> None:
        self.dsn = dsn
        self.table = table
        self.schema = schema
        self._ensure_table()

    def _ensure_table(self) -> None:
        with connect(self.dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute(
                    SQL("CREATE SCHEMA IF NOT EXISTS {}").format(Identifier(self.schema))
                )
                cur.execute(
                    SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}.{} (
                            id TEXT PRIMARY KEY,
                            embedding VECTOR NOT NULL,
                            metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    ).format(Identifier(self.schema), Identifier(self.table))
                )

    def get_metadatas(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        with connect(self.dsn, autocommit=True, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    SQL("SELECT id, metadata FROM {}.{} WHERE id = ANY(%s)").format(
                        Identifier(self.schema), Identifier(self.table)
                    ),
                    (ids,),
                )
                rows = cur.fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            rid = str(row.get("id") or "")
            meta = row.get("metadata")
            out[rid] = meta if isinstance(meta, dict) else {}
        return out

    def upsert_embeddings(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        with connect(self.dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                for record in records:
                    rid = str(record.get("id") or "").strip()
                    emb = record.get("embedding")
                    meta = record.get("metadata") or {}
                    if not rid or not isinstance(emb, list) or not emb:
                        continue
                    cur.execute(
                        SQL(
                            """
                            INSERT INTO {}.{} (id, embedding, metadata, updated_at)
                            VALUES (%s, %s::vector, %s::jsonb, NOW())
                            ON CONFLICT (id)
                            DO UPDATE SET
                                embedding = EXCLUDED.embedding,
                                metadata = EXCLUDED.metadata,
                                updated_at = NOW()
                            """
                        ).format(Identifier(self.schema), Identifier(self.table)),
                        (rid, _to_vector_literal(emb), json.dumps(meta, ensure_ascii=False)),
                    )

    def query(
        self,
        embedding: list[float],
        top_k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not embedding:
            return []
        vec = _to_vector_literal(embedding)
        scalar = dict(filters or {})
        where_parts: list[str] = ["1=1"]
        params: list[Any] = []
        account_id = str(scalar.get("account_id") or "").strip()
        if account_id:
            where_parts.append("COALESCE(metadata->>'account_id', '') = %s")
            params.append(account_id)
        scopes_raw = scalar.get("scopes")
        scopes = [str(x).strip().lower() for x in (scopes_raw or []) if str(x).strip()]
        if scopes:
            where_parts.append("LOWER(COALESCE(metadata->>'scope', 'agent')) = ANY(%s)")
            params.append(scopes)
        owners_raw = scalar.get("owner_spaces")
        owners = [str(x).strip() for x in (owners_raw or []) if str(x).strip()]
        if owners:
            where_parts.append("COALESCE(metadata->>'owner_space', metadata->>'agent_id', '') = ANY(%s)")
            params.append(owners)
        excluded_statuses_raw = scalar.get("exclude_statuses")
        excluded_statuses = [
            str(x).strip().lower() for x in (excluded_statuses_raw or []) if str(x).strip()
        ]
        if excluded_statuses:
            where_parts.append(
                "LOWER(COALESCE(metadata->>'status', metadata->>'lifecycle_status', 'active')) <> ALL(%s)"
            )
            params.append(excluded_statuses)
        where_sql = " AND ".join(where_parts)
        with connect(self.dsn, autocommit=True, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    SQL(
                        """
                        SELECT id, metadata, (embedding <-> %s::vector) AS distance
                        FROM {}.{}
                        WHERE """
                        + where_sql
                        + """
                        ORDER BY embedding <-> %s::vector ASC
                        LIMIT %s
                        """
                    ).format(Identifier(self.schema), Identifier(self.table)),
                    (vec, *params, vec, max(1, int(top_k))),
                )
                rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "id": str(row.get("id") or ""),
                    "metadata": row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
                    "distance": row.get("distance"),
                }
            )
        return out
