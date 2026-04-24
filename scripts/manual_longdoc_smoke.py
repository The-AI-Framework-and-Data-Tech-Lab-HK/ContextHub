#!/usr/bin/env python3
"""Manual smoke test for Task 6 long-document retrieval.

Usage:
  python scripts/manual_longdoc_smoke.py
  python scripts/manual_longdoc_smoke.py --source /path/to/doc.md
  python scripts/manual_longdoc_smoke.py --source /path/to/file.pdf --uri ctx://resources/manuals/my-doc
  python scripts/manual_longdoc_smoke.py --source /path/to/file.pdf --query "postgres replication wal lag"

Prerequisites:
  - PostgreSQL is running
  - alembic upgrade head
  - ContextHub/.env contains OPENAI_API_KEY for a real success-path run

This script does not require the HTTP server. It boots the FastAPI lifespan,
uses app.state.document_ingester directly, provisions root-team write access
for the selected agent, ingests one document, reads L0/L1/L2 back, then runs
RetrievalService.search() to verify the long-document precision path.
The default runtime prefers tree retrieval and may fall back to keyword
retrieval if tree selection returns a low-confidence snippet.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path
import re
import tempfile

from contexthub.main import app
from contexthub.models.context import ContextLevel
from contexthub.models.request import RequestContext
from contexthub.models.search import SearchRequest

ROOT_TEAM_ID = "00000000-0000-0000-0000-000000000001"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a manual Task 6 smoke test.")
    parser.add_argument(
        "--source",
        help="Optional path to a .txt, .md, or .pdf document. If omitted, a sample markdown file is generated.",
    )
    parser.add_argument(
        "--uri",
        help="Canonical resource URI. If omitted, a unique ctx://resources/manuals/... URI is generated.",
    )
    parser.add_argument("--account-id", default="acme", help="Tenant/account id.")
    parser.add_argument("--agent-id", default="query-agent", help="Agent id used for ingestion.")
    parser.add_argument(
        "--query",
        help=(
            "Optional search query used after ingestion. If omitted, the script derives a "
            "discovery-friendly query from the stored l0/l1 content."
        ),
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="top_k for the retrieval smoke search (default: 5).",
    )
    parser.add_argument(
        "--disable-llm-tree",
        action="store_true",
        help="Skip LLM tree construction and force deterministic heading/chunk fallback.",
    )
    return parser.parse_args()


def ensure_source_file(source: str | None) -> Path:
    if source:
        return Path(source).resolve()

    temp_dir = Path(tempfile.mkdtemp(prefix="contexthub-longdoc-"))
    sample_path = temp_dir / "sample_long_document.md"
    sample_path.write_text(
        """# ContextHub Long Document Smoke Test

## Overview
This sample document exercises long-document ingestion and retrieval end to end.

## Why It Exists
The script should create a resource context, persist extracted text on disk,
build a section tree, read L0, L1, and L2 back through the ContextStore,
and then verify that search returns a focused snippet via the tree path.

## Retrieval Verification
The search query should explicitly match the generated retrieval summaries.
This sample includes terms like task6, long document, tree retrieval, and focused snippet.

## Expected Outcome
If OPENAI_API_KEY is configured, ingestion should succeed and create a focused search result
whose retrieval_strategy is tree and whose snippet is non-empty.
""",
        encoding="utf-8",
    )
    return sample_path


def derive_search_query(explicit_query: str | None, l0: str | None, l1: str | None) -> str:
    if explicit_query:
        return explicit_query

    text = " ".join(part for part in (l0, l1) if part)
    tokens: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9]+", text):
        lowered = token.lower()
        if len(lowered) < 4 or lowered in seen:
            continue
        seen.add(lowered)
        tokens.append(token)
        if len(tokens) == 5:
            break
    if not tokens:
        return "task6 long document retrieval snippet"
    return " ".join(tokens)


async def ensure_root_team_membership(db, agent_id: str) -> None:
    await db.execute(
        """
        INSERT INTO team_memberships (agent_id, team_id, role, access)
        VALUES ($1, $2::uuid, 'member', 'read_write')
        ON CONFLICT (agent_id, team_id)
        DO UPDATE SET access = 'read_write'
        """,
        agent_id,
        ROOT_TEAM_ID,
    )


async def main() -> None:
    args = parse_args()
    source_path = ensure_source_file(args.source)
    uri = args.uri or (
        "ctx://resources/manuals/manual-smoke-"
        + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    )
    ctx = RequestContext(account_id=args.account_id, agent_id=args.agent_id)

    print("Preparing manual long-document smoke test...")
    print(f"  account_id: {args.account_id}")
    print(f"  agent_id:   {args.agent_id}")
    print(f"  source:     {source_path}")
    print(f"  uri:        {uri}")
    print(f"  llm_tree:   {not args.disable_llm_tree}")

    async with app.router.lifespan_context(app):
        repo = app.state.repo
        ingester = app.state.document_ingester
        context_store = app.state.context_store
        retrieval_service = app.state.retrieval_service

        async with repo.session(args.account_id) as db:
            await ensure_root_team_membership(db, args.agent_id)

            response = await ingester.ingest(
                db,
                uri,
                str(source_path),
                ctx,
                tags=["manual-smoke", "task5"],
                allow_llm_tree=not args.disable_llm_tree,
            )
            print("\nIngest succeeded:")
            print(f"  context_id:    {response.context_id}")
            print(f"  section_count: {response.section_count}")
            print(f"  file_path:     {response.file_path}")

            row = await db.fetchrow(
                """
                SELECT l0_content, l1_content, file_path, status, l2_content
                FROM contexts
                WHERE uri = $1
                """,
                response.uri,
            )
            if row is None:
                raise RuntimeError("Expected inserted context row was not found")

            l0 = await context_store.read(db, response.uri, ContextLevel.L0, ctx)
            l1 = await context_store.read(db, response.uri, ContextLevel.L1, ctx)
            l2 = await context_store.read(db, response.uri, ContextLevel.L2, ctx)
            section_rows = await db.fetch(
                """
                SELECT node_id, title, depth, start_offset, end_offset
                FROM document_sections
                WHERE context_id = $1
                ORDER BY section_id
                """,
                response.context_id,
            )

            print("\nContext row:")
            print(f"  status:        {row['status']}")
            print(f"  file_path:     {row['file_path']}")
            print(f"  l2_content:    {row['l2_content']!r} (should be None)")

            print("\nReadback:")
            print(f"  L0: {l0[:120]}")
            print(f"  L1: {l1[:200]}")
            print(f"  L2 preview: {l2[:300]}")

            print("\nDocument sections:")
            for section in section_rows[:10]:
                print(
                    "  - "
                    f"{section['node_id']} depth={section['depth']} "
                    f"title={section['title']!r} "
                    f"offsets=({section['start_offset']}, {section['end_offset']})"
                )
            if len(section_rows) > 10:
                print(f"  ... {len(section_rows) - 10} more sections")

            query = derive_search_query(args.query, row["l0_content"], row["l1_content"])
            print("\nSearch smoke:")
            print(f"  query:        {query}")
            search_response = await retrieval_service.search(
                db,
                SearchRequest(query=query, top_k=args.top_k, level=ContextLevel.L1),
                ctx,
            )
            print(f"  retrieval_id: {search_response.retrieval_id}")
            print(f"  total:        {search_response.total}")

            target = next((result for result in search_response.results if result.uri == response.uri), None)
            if target is None:
                raise RuntimeError(
                    "Search did not return the ingested long document. "
                    "Try a more explicit --query matching the printed L0/L1 text."
                )
            if not target.snippet:
                raise RuntimeError("Search returned the document but snippet is empty")
            if target.retrieval_strategy not in {"tree", "keyword"}:
                raise RuntimeError(
                    "Expected retrieval_strategy to be 'tree' or 'keyword', "
                    f"got {target.retrieval_strategy!r}"
                )
            if target.section_id is None:
                raise RuntimeError("Expected a non-empty section_id for the tree retrieval path")

            print("\nMatched search result:")
            print(f"  uri:                {target.uri}")
            print(f"  score:              {target.score:.4f}")
            print(f"  retrieval_strategy: {target.retrieval_strategy}")
            print(f"  section_id:         {target.section_id}")
            print(f"  snippet preview:    {target.snippet[:300]}")

            print("\nManual Task 6 smoke test completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
