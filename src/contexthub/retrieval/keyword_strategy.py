"""Keyword fallback search when embedding is unavailable."""

from __future__ import annotations

import re

from contexthub.db.repository import ScopedRepo


async def keyword_search(
    db: ScopedRepo,
    query: str,
    top_k: int,
    context_types: list[str] | None = None,
    scopes: list[str] | None = None,
    include_stale: bool = False,
    only_non_fresh: bool = False,
) -> list[dict]:
    """Keyword fallback: ILIKE matching on l0_content/l1_content.

    ``only_non_fresh`` restricts the result to rows that are *not* active+fresh.
    It exists for the stale-notice pass; see the comment on the filter below.
    """
    keywords = re.findall(r"\w+", query.lower())
    if not keywords:
        return []

    conditions = [
        # Explicit tenant filter: don't rely on RLS alone (bypassed for superuser roles).
        "account_id = current_setting('app.account_id')",
        "status NOT IN ('archived', 'deleted')",
    ]
    params: list = []
    idx = 1

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

    # Build ILIKE match count expression
    match_exprs = []
    for kw in keywords:
        params.append(f"%{kw}%")
        match_exprs.append(
            f"(CASE WHEN LOWER(COALESCE(l0_content,'')) LIKE ${idx} THEN 1 ELSE 0 END"
            f" + CASE WHEN LOWER(COALESCE(l1_content,'')) LIKE ${idx} THEN 1 ELSE 0 END)"
        )
        idx += 1

    score_expr = " + ".join(match_exprs)
    max_score = len(keywords) * 2  # max possible matches

    where = " AND ".join(conditions)
    params.append(top_k)

    rows = await db.fetch(
        f"""
        SELECT id, uri, context_type, scope, owner_space, status, version,
               validity_status, validity_reason,
               l0_content, l1_content, tags, file_path,
               ({score_expr})::float / {max_score} AS cosine_similarity
        FROM contexts
        WHERE {where} AND ({score_expr}) > 0
        ORDER BY (status = 'active' AND validity_status = 'fresh') DESC,
                 ({score_expr}) DESC
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
