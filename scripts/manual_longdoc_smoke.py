#!/usr/bin/env python3
"""Manual smoke test for Task 5 long-document ingestion.

Usage:
  python scripts/manual_longdoc_smoke.py
  python scripts/manual_longdoc_smoke.py --source /path/to/doc.md
  python scripts/manual_longdoc_smoke.py --source /path/to/file.pdf --uri ctx://resources/manuals/my-doc

Prerequisites:
  - PostgreSQL is running
  - alembic upgrade head
  - ContextHub/.env contains OPENAI_API_KEY for a real success-path run

This script does not require the HTTP server. It boots the FastAPI lifespan,
uses app.state.document_ingester directly, provisions root-team write access
for the selected agent, ingests one document, and reads L0/L1/L2 back.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path
import tempfile

from contexthub.main import app
from contexthub.models.context import ContextLevel
from contexthub.models.request import RequestContext

ROOT_TEAM_ID = "00000000-0000-0000-0000-000000000001"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a manual Task 5 smoke test.")
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
    return parser.parse_args()


def ensure_source_file(source: str | None) -> Path:
    if source:
        return Path(source).resolve()

    temp_dir = Path(tempfile.mkdtemp(prefix="contexthub-longdoc-"))
    sample_path = temp_dir / "sample_long_document.md"
    sample_path.write_text(
        """# ContextHub Long Document Smoke Test

## Overview
This sample document exercises long-document ingestion end to end.

## Why It Exists
The script should create a resource context, persist extracted text on disk,
build a section tree, and read L0, L1, and L2 back through the ContextStore.

## Expected Outcome
If OPENAI_API_KEY is configured, ingestion should succeed and create:
- a contexts row
- document_sections rows
- a created change_event
- extracted.txt and extracted.md files
""",
        encoding="utf-8",
    )
    return sample_path


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

    async with app.router.lifespan_context(app):
        repo = app.state.repo
        ingester = app.state.document_ingester
        context_store = app.state.context_store

        async with repo.session(args.account_id) as db:
            await ensure_root_team_membership(db, args.agent_id)

            response = await ingester.ingest(
                db,
                uri,
                str(source_path),
                ctx,
                tags=["manual-smoke", "task5"],
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

            print("\nManual smoke test completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
