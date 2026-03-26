#!/usr/bin/env python3
"""ContextHub MVP End-to-End Demo

Prerequisites:
  docker-compose up -d
  alembic upgrade head
  uvicorn contexthub.main:app --port 8000

Usage:
  python scripts/demo_e2e.py
"""

from __future__ import annotations

import asyncio
import sys
import time

import httpx


BASE_URL = "http://localhost:8000"
API_KEY = "changeme"
ACCOUNT = "acme"


def _headers(agent_id: str) -> dict:
    return {
        "X-API-Key": API_KEY,
        "X-Account-Id": ACCOUNT,
        "X-Agent-Id": agent_id,
    }


def step(n: int, desc: str):
    print(f"\n{'='*60}")
    print(f"  Step {n}: {desc}")
    print(f"{'='*60}")


async def _ensure_team_membership():
    """Ensure query-agent is a direct member of engineering (seed data only has engineering/backend)."""
    import asyncpg
    conn = await asyncpg.connect("postgresql://contexthub:contexthub@localhost:5432/contexthub")
    try:
        await conn.execute("SET app.account_id = 'acme'")
        await conn.execute("""
            INSERT INTO team_memberships (agent_id, team_id, role, access, is_primary)
            VALUES ('query-agent', '00000000-0000-0000-0000-000000000002', 'member', 'read_write', FALSE)
            ON CONFLICT DO NOTHING
        """)
    finally:
        await conn.close()


async def main():
    await _ensure_team_membership()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as http:
        # Health check
        r = await http.get("/health")
        if r.status_code != 200:
            print("Server not reachable. Start it first.")
            sys.exit(1)
        print("Server healthy.")

        qa = _headers("query-agent")
        aa = _headers("analysis-agent")

        # ── Step 1: query-agent writes private memory ──
        step(1, "query-agent writes private memory")
        r = await http.post("/api/v1/memories", json={
            "content": "The orders table uses user_id as FK to users. Always JOIN on orders.user_id = users.id.",
            "tags": ["schema-note", "orders"],
        }, headers=qa)
        assert r.status_code == 201, f"Expected 201, got {r.status_code}: {r.text}"
        mem = r.json()
        mem_uri = mem["uri"]
        print(f"  Created: {mem_uri}")

        # ── Step 2: query-agent creates and publishes Skill v1 ──
        step(2, "query-agent publishes sql-generator Skill v1")
        # Create skill context first
        r = await http.post("/api/v1/contexts", json={
            "uri": "ctx://team/engineering/skills/sql-generator",
            "context_type": "skill",
            "scope": "team",
            "owner_space": "engineering",
            "l2_content": "SELECT * FROM orders WHERE status = 'completed'",
        }, headers=qa)
        assert r.status_code == 201, f"Expected 201, got {r.status_code}: {r.text}"
        skill_ctx = r.json()
        print(f"  Skill context: {skill_ctx['uri']}")

        r = await http.post("/api/v1/skills/versions", json={
            "skill_uri": "ctx://team/engineering/skills/sql-generator",
            "content": "v1: Basic SQL generator for orders queries",
            "changelog": "Initial release",
            "is_breaking": False,
        }, headers=qa)
        assert r.status_code == 201, f"Expected 201, got {r.status_code}: {r.text}"
        v1 = r.json()
        print(f"  Published v{v1['version']}")

        # ── Step 3: query-agent promotes memory to team ──
        step(3, "query-agent promotes memory to team/engineering")
        r = await http.post("/api/v1/memories/promote", json={
            "uri": mem_uri,
            "target_team": "engineering",
        }, headers=qa)
        assert r.status_code == 201, f"Expected 201, got {r.status_code}: {r.text}"
        promoted = r.json()
        print(f"  Promoted to: {promoted['uri']}")

        # ── Step 4: analysis-agent sees shared memory + subscribes to skill ──
        step(4, "analysis-agent retrieves shared memory and subscribes to skill")
        r = await http.get("/api/v1/memories", headers=aa)
        assert r.status_code == 200
        memories = r.json()
        shared = [m for m in memories if "shared_knowledge" in m["uri"]]
        print(f"  analysis-agent sees {len(shared)} shared memories")
        assert len(shared) >= 1, "Promoted memory not visible!"

        r = await http.post("/api/v1/skills/subscribe", json={
            "skill_uri": "ctx://team/engineering/skills/sql-generator",
            "pinned_version": 1,
        }, headers=aa)
        assert r.status_code == 200, f"Subscribe failed: {r.text}"
        print(f"  Subscribed to sql-generator, pinned v1")

        # ── Step 5: query-agent publishes breaking Skill v2 ──
        step(5, "query-agent publishes breaking Skill v2")
        r = await http.post("/api/v1/skills/versions", json={
            "skill_uri": "ctx://team/engineering/skills/sql-generator",
            "content": "v2: Rewritten SQL generator with CTE support",
            "changelog": "Breaking: new output format",
            "is_breaking": True,
        }, headers=qa)
        assert r.status_code == 201
        v2 = r.json()
        print(f"  Published v{v2['version']} (breaking)")

        # ── Step 6: Wait for propagation + verify ──
        step(6, "Verify propagation: stale/advisory")
        await asyncio.sleep(2)  # Let propagation engine process

        # analysis-agent reads skill — should get v1 with advisory about v2
        r = await http.post("/api/v1/tools/read", json={
            "uri": "ctx://team/engineering/skills/sql-generator",
        }, headers=aa)
        assert r.status_code == 200
        read_result = r.json()
        print(f"  analysis-agent reads skill: version={read_result.get('version')}")
        if read_result.get("advisory"):
            print(f"  Advisory: {read_result['advisory']}")

        # ── Step 7 (optional): Catalog sync + sql-context ──
        step(7, "[Vertical] Catalog sync + sql-context query")
        r = await http.post("/api/v1/datalake/sync", json={"catalog": "mock"}, headers=qa)
        if r.status_code == 200:
            sync = r.json()
            print(f"  Synced {sync['tables_synced']} tables ({sync['tables_created']} new)")

            r = await http.get("/api/v1/datalake/mock/prod", headers=qa)
            if r.status_code == 200:
                tables = r.json().get("tables", [])
                print(f"  Listed {len(tables)} tables in mock/prod")

            r = await http.post("/api/v1/search/sql-context", json={
                "query": "How many orders per user?",
                "catalog": "mock",
                "top_k": 3,
            }, headers=qa)
            if r.status_code == 200:
                sql_ctx = r.json()
                print(f"  sql-context returned {sql_ctx['total_tables_found']} relevant tables")
            else:
                print(f"  sql-context: {r.status_code} (search may need embeddings)")
        else:
            print(f"  Sync returned {r.status_code}: {r.text}")

        # ── Summary ──
        print(f"\n{'='*60}")
        print("  MVP Demo Complete")
        print(f"{'='*60}")
        print(f"  - Private memory created: {mem_uri}")
        print(f"  - Promoted to team: {promoted['uri']}")
        print(f"  - Skill v1 + v2 published, breaking propagation triggered")
        print(f"  - Cross-agent visibility verified")
        print(f"  - Catalog sync + sql-context demonstrated")


if __name__ == "__main__":
    asyncio.run(main())
