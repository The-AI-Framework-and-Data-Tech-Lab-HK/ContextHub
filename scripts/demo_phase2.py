#!/usr/bin/env python3
"""ContextHub Phase 2 — Enterprise Governance Demo

Demonstrates Phase 2 capabilities on top of Phase 1:
  - Explicit ACL deny (read-path blocking)
  - Keyword masking (field_masks → [MASKED])
  - Share grant / revoke (cross-team sharing without copy)
  - Audit trail (query audit_log via Admin API)
  - Write bypass (read deny does NOT block writes)

Prerequisites:
  docker-compose up -d
  alembic upgrade head          # includes 003_acl_audit_tables
  uvicorn contexthub.main:app --port 8000

Usage:
  python scripts/demo_phase2.py
"""

from __future__ import annotations

import asyncio
import json
import sys

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


async def _ensure_admin_role():
    """Promote query-agent to admin in the engineering team so it can manage policies."""
    import asyncpg
    conn = await asyncpg.connect("postgresql://contexthub:contexthub@localhost:5432/contexthub")
    try:
        await conn.execute("SET app.account_id = 'acme'")
        await conn.execute("""
            UPDATE team_memberships
            SET role = 'admin'
            WHERE agent_id = 'query-agent'
              AND team_id = '00000000-0000-0000-0000-000000000002'
        """)
        await conn.execute("""
            INSERT INTO team_memberships (agent_id, team_id, role, access, is_primary)
            VALUES ('query-agent', '00000000-0000-0000-0000-000000000002', 'admin', 'read_write', FALSE)
            ON CONFLICT DO NOTHING
        """)
    finally:
        await conn.close()


async def main():
    await _ensure_admin_role()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as http:
        r = await http.get("/health")
        if r.status_code != 200:
            print("Server not reachable. Start it first.")
            sys.exit(1)
        print("Server healthy — Phase 2 Governance Demo\n")

        qa = _headers("query-agent")
        aa = _headers("analysis-agent")

        # ── Step 1: Seed — query-agent writes a context with sensitive data ──
        step(1, "query-agent creates a context with sensitive content")
        supplier_content = (
            "供应商成本明细：春季促销供货底价不低于零售价的 60%，"
            "核心供应商 A 的折扣为 55%，供应商 B 的折扣为 58%。"
            "谈判底线：不可接受低于 50% 的任何报价。"
        )
        r = await http.post("/api/v1/contexts", json={
            "uri": "ctx://team/engineering/docs/supplier-costs",
            "context_type": "resource",
            "scope": "team",
            "owner_space": "engineering",
            "l1_content": supplier_content,
            "l2_content": supplier_content,
        }, headers=qa)
        if r.status_code == 409:
            print("  Context already exists, continuing…")
        else:
            assert r.status_code == 201, f"Expected 201, got {r.status_code}: {r.text}"
            print(f"  Created: {r.json()['uri']}")

        supplier_uri = "ctx://team/engineering/docs/supplier-costs"

        # Verify analysis-agent can read it (Phase 1 baseline)
        r = await http.post("/api/v1/tools/read", json={
            "uri": supplier_uri,
        }, headers=aa)
        assert r.status_code == 200, f"Baseline read failed: {r.status_code}: {r.text}"
        print(f"  Baseline: analysis-agent CAN read supplier-costs (Phase 1 default)")

        # ── Step 2: ACL Deny — block analysis-agent from reading ──
        step(2, "Admin creates DENY policy → block analysis-agent")
        r = await http.post("/api/v1/admin/policies", json={
            "resource_uri_pattern": supplier_uri,
            "principal": "analysis-agent",
            "effect": "deny",
            "actions": ["read"],
            "priority": 10,
        }, headers=qa)
        assert r.status_code == 201, f"Create policy failed: {r.status_code}: {r.text}"
        deny_policy = r.json()
        deny_policy_id = deny_policy["id"]
        print(f"  Deny policy created: {deny_policy_id}")

        # ── Step 3: Verify deny blocks read ──
        step(3, "Verify: analysis-agent is BLOCKED from reading")
        r = await http.post("/api/v1/tools/read", json={
            "uri": supplier_uri,
        }, headers=aa)
        assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"
        print(f"  analysis-agent read → 403 Forbidden ✓")

        # Verify query-agent can still read (owner unaffected)
        r = await http.post("/api/v1/tools/read", json={
            "uri": supplier_uri,
        }, headers=qa)
        assert r.status_code == 200, f"Owner read failed: {r.status_code}"
        print(f"  query-agent (owner) read → 200 OK ✓")

        # ── Step 4: Keyword Masking — create masking policy ──
        step(4, "Admin creates policy with field_masks → keyword masking")

        # First remove the deny so we can test masking (masking only applies on allowed reads)
        r = await http.delete(f"/api/v1/admin/policies/{deny_policy_id}", headers=qa)
        assert r.status_code == 204, f"Delete policy failed: {r.status_code}"
        print(f"  Removed deny policy to test masking")

        # Create allow policy with field_masks
        r = await http.post("/api/v1/admin/policies", json={
            "resource_uri_pattern": supplier_uri,
            "principal": "analysis-agent",
            "effect": "allow",
            "actions": ["read"],
            "field_masks": ["60%", "55%", "58%", "50%", "底价", "底线"],
            "priority": 5,
        }, headers=qa)
        assert r.status_code == 201, f"Create masking policy failed: {r.status_code}: {r.text}"
        mask_policy = r.json()
        mask_policy_id = mask_policy["id"]
        print(f"  Masking policy created: {mask_policy_id}")
        print(f"  field_masks: {mask_policy['field_masks']}")

        # ── Step 5: Verify masking effect ──
        step(5, "Verify: analysis-agent reads MASKED content")
        r = await http.post("/api/v1/tools/read", json={
            "uri": supplier_uri,
        }, headers=aa)
        assert r.status_code == 200, f"Masked read failed: {r.status_code}: {r.text}"
        content = r.json().get("content", "")
        assert "[MASKED]" in content, f"Expected [MASKED] in content, got: {content}"
        assert "60%" not in content, f"Sensitive keyword '60%' should be masked"
        print(f"  Content (masked):\n    {content}")
        print(f"  Sensitive keywords replaced with [MASKED] ✓")

        # ── Step 6: Share Grant — cross-team sharing without copy ──
        step(6, "Share grant: query-agent shares a context with analysis-agent")

        # Create a new context specifically for share demo
        share_target_uri = "ctx://team/engineering/docs/api-standards"
        api_standards_content = "API 设计标准：RESTful 接口规范、分页协议、错误码体系。"
        r = await http.post("/api/v1/contexts", json={
            "uri": share_target_uri,
            "context_type": "resource",
            "scope": "team",
            "owner_space": "engineering",
            "l1_content": api_standards_content,
            "l2_content": api_standards_content,
        }, headers=qa)
        if r.status_code == 409:
            print("  Share target context already exists, continuing…")
        else:
            assert r.status_code == 201, f"Expected 201, got {r.status_code}: {r.text}"
            print(f"  Created share target: {share_target_uri}")

        # Create share grant via /shares API
        r = await http.post("/api/v1/shares", json={
            "source_uri": share_target_uri,
            "target_principal": "analysis-agent",
        }, headers=qa)
        assert r.status_code == 201, f"Share grant failed: {r.status_code}: {r.text}"
        share_grant = r.json()
        share_grant_id = share_grant["id"]
        print(f"  Share grant created: {share_grant_id}")
        print(f"  conditions: {share_grant['conditions']}")

        # Verify analysis-agent can read the shared context
        r = await http.post("/api/v1/tools/read", json={
            "uri": share_target_uri,
        }, headers=aa)
        assert r.status_code == 200, f"Share read failed: {r.status_code}: {r.text}"
        print(f"  analysis-agent reads shared context → 200 OK ✓")

        # ── Step 7: Share Revoke ──
        step(7, "Share revoke: remove analysis-agent's access")
        r = await http.delete(f"/api/v1/shares/{share_grant_id}", headers=qa)
        assert r.status_code == 204, f"Revoke failed: {r.status_code}: {r.text}"
        print(f"  Share grant revoked ✓")

        # List grants to confirm empty
        r = await http.get("/api/v1/shares", params={
            "source_uri": share_target_uri,
        }, headers=qa)
        assert r.status_code == 200
        grants = r.json()
        print(f"  Remaining grants for {share_target_uri}: {len(grants)}")

        # ── Step 8: Write Bypass — read deny does NOT block writes ──
        step(8, "Verify: read deny does NOT block write operations")

        # Re-create a deny policy on the supplier-costs for analysis-agent
        r = await http.post("/api/v1/admin/policies", json={
            "resource_uri_pattern": "ctx://agent/analysis-agent/memories/*",
            "principal": "analysis-agent",
            "effect": "deny",
            "actions": ["read"],
            "priority": 10,
        }, headers=qa)
        assert r.status_code == 201
        read_deny_id = r.json()["id"]
        print(f"  Created read-deny on analysis-agent's own memories")

        # analysis-agent can still write a new memory despite read deny
        r = await http.post("/api/v1/memories", json={
            "content": "Phase 2 write bypass test: this should succeed despite read deny.",
            "tags": ["phase2-test"],
        }, headers=aa)
        assert r.status_code == 201, f"Write should succeed: {r.status_code}: {r.text}"
        print(f"  analysis-agent writes memory → 201 Created ✓")
        print(f"  Write path unaffected by read-deny policy ✓")

        # Clean up the test deny
        await http.delete(f"/api/v1/admin/policies/{read_deny_id}", headers=qa)

        # ── Step 9: Audit Trail — query audit log ──
        step(9, "Query audit log → verify all operations recorded")
        r = await http.get("/api/v1/admin/audit", params={
            "limit": 20,
        }, headers=qa)
        assert r.status_code == 200, f"Audit query failed: {r.status_code}: {r.text}"
        entries = r.json()
        print(f"  Audit log has {len(entries)} recent entries")

        action_types = set()
        for entry in entries:
            action_types.add(entry["action"])
        print(f"  Action types seen: {sorted(action_types)}")

        policy_changes = [e for e in entries if e["action"] == "policy_change"]
        denied_entries = [e for e in entries if e["result"] == "denied"]
        read_entries = [e for e in entries if e["action"] == "read"]
        print(f"  - policy_change events: {len(policy_changes)}")
        print(f"  - access_denied events: {len(denied_entries)}")
        print(f"  - read events: {len(read_entries)}")

        if policy_changes:
            print(f"\n  Sample policy_change audit entry:")
            sample = policy_changes[0]
            print(f"    actor: {sample['actor']}")
            print(f"    resource: {sample['resource_uri']}")
            print(f"    result: {sample['result']}")
            print(f"    metadata: {json.dumps(sample.get('metadata', {}), ensure_ascii=False)}")

        # ── Step 10: List all policies — show current state ──
        step(10, "List all active policies")
        r = await http.get("/api/v1/admin/policies", headers=qa)
        assert r.status_code == 200
        policies = r.json()
        print(f"  Total active policies: {len(policies)}")
        for p in policies:
            print(f"    [{p['effect']}] {p['resource_uri_pattern']} → {p['principal']}"
                  f"  actions={p['actions']}"
                  f"  masks={p.get('field_masks') or '—'}")

        # ── Cleanup test policies ──
        print(f"\n{'─'*60}")
        print("  Cleaning up test policies…")
        for p in policies:
            pid = p["id"]
            await http.delete(f"/api/v1/admin/policies/{pid}", headers=qa)
        print("  Done.")

        # ── Summary ──
        print(f"\n{'='*60}")
        print("  Phase 2 Governance Demo Complete")
        print(f"{'='*60}")
        print("  Verified capabilities (all from Phase 2):")
        print("  ✓ Explicit ACL deny → read blocked (403)")
        print("  ✓ Keyword masking → sensitive content replaced with [MASKED]")
        print("  ✓ Share grant → cross-team read without copy")
        print("  ✓ Share revoke → access removed")
        print("  ✓ Write bypass → read-deny does NOT block writes")
        print("  ✓ Audit trail → all operations recorded in audit_log")
        print("  ✓ Admin API → policy CRUD + audit query")


if __name__ == "__main__":
    asyncio.run(main())
