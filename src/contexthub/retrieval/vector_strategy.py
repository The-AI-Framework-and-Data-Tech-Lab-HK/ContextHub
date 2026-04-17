"""pgvector cosine similarity search."""

from __future__ import annotations

from contexthub.db.repository import ScopedRepo


async def vector_search(
    db: ScopedRepo,
    query_embedding: list[float],
    top_k: int,
    context_types: list[str] | None = None,
    scopes: list[str] | None = None,
    include_stale: bool = True,
) -> list[dict]:
    """pgvector cosine similarity search. Returns candidates with cosine_similarity score."""
    embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    conditions = [
        "l0_embedding IS NOT NULL",
        "status NOT IN ('archived', 'deleted')",
    ]
    params: list = [embedding_str]
    idx = 2

    if not include_stale:
        conditions.append("status != 'stale'")

    if context_types:
        conditions.append(f"context_type = ANY(${idx})")
        params.append(context_types)
        idx += 1

    if scopes:
        conditions.append(f"scope = ANY(${idx})")
        params.append(scopes)
        idx += 1

    params.append(top_k)
    where = " AND ".join(conditions)

    rows = await db.fetch(
        f"""
        SELECT id, uri, context_type, scope, owner_space, status, version,
               l0_content, l1_content, tags, file_path,
               1 - (l0_embedding <=> $1::vector) AS cosine_similarity
        FROM contexts
        WHERE {where}
        ORDER BY l0_embedding <=> $1::vector
        LIMIT ${idx}
        """,
        *params,
    )

    return [
        {
            "id": r["id"],
            "uri": r["uri"],
            "context_type": r["context_type"],
            "scope": r["scope"],
            "owner_space": r["owner_space"],
            "status": r["status"],
            "version": r["version"],
            "l0_content": r["l0_content"],
            "l1_content": r["l1_content"],
            "tags": list(r["tags"] or []),
            "file_path": r.get("file_path"),
            "cosine_similarity": float(r["cosine_similarity"]),
        }
        for r in rows
    ]
