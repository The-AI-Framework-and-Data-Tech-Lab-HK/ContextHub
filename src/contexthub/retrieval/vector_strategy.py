"""pgvector cosine similarity search."""

from __future__ import annotations

from contexthub.db.repository import ScopedRepo


async def vector_search(
    db: ScopedRepo,
    query_embedding: list[float],
    top_k: int,
    context_types: list[str] | None = None,
    scopes: list[str] | None = None,
    include_stale: bool = False,
    only_non_fresh: bool = False,
) -> list[dict]:
    """pgvector cosine similarity search. Returns candidates with cosine_similarity score.

    ``only_non_fresh`` restricts the result to rows that are *not* active+fresh.
    It exists for the stale-notice pass; see the comment on the filter below.
    """
    embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    conditions = [
        # Explicit tenant filter: don't rely on RLS alone (bypassed for superuser roles).
        "account_id = current_setting('app.account_id')",
        "l0_embedding IS NOT NULL",
        "status NOT IN ('archived', 'deleted')",
    ]
    params: list = [embedding_str]
    idx = 2

    # A fresh-first ORDER BY plus LIMIT means a stale row ranks below every fresh
    # match and is dropped whenever the fresh matches alone fill the limit. That
    # silently removes the very rows a stale-notice pass needs to explain, so the
    # notice pass asks for the complement of the fresh set instead of competing
    # with it for the same slots.
    if only_non_fresh:
        conditions.append("NOT (status = 'active' AND validity_status = 'fresh')")
    elif not include_stale:
        conditions.append("status = 'active'")
        conditions.append("validity_status = 'fresh'")

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
               validity_status, validity_reason,
               l0_content, l1_content, tags, file_path,
               1 - (l0_embedding <=> $1::vector) AS cosine_similarity
        FROM contexts
        WHERE {where}
        ORDER BY (status = 'active' AND validity_status = 'fresh') DESC,
                 l0_embedding <=> $1::vector
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
            "validity_status": r.get("validity_status", "fresh"),
            "validity_reason": r.get("validity_reason"),
            "l0_content": r["l0_content"],
            "l1_content": r["l1_content"],
            "tags": list(r["tags"] or []),
            "file_path": r.get("file_path"),
            "cosine_similarity": float(r["cosine_similarity"]),
        }
        for r in rows
    ]
