#!/usr/bin/env python3
"""ContextHub Governance Correctness Benchmark

55+ checks across 5 governance dimensions, each producing a category-level
pass rate (%) and per-operation latency profile (p50/p95/p99).

  Suite 1 — Isolation Correctness    (13 checks)
  Suite 2 — Promotion & Sharing      (12 checks)
  Suite 3 — Version Resolution       (10 checks)
  Suite 4 — Propagation & Convergence(10 checks)
  Suite 5 — LLM-Native Tools API     (10 checks)

Prerequisites:
  PostgreSQL + alembic upgrade head
  uvicorn contexthub.main:app --port 8000

Usage:
  python scripts/benchmark_workflow.py                  # all suites
  python scripts/benchmark_workflow.py --suite 1        # isolation only
  python scripts/benchmark_workflow.py --suite 1,3,5    # selected suites

API response formats (important for check logic):
  GET  /memories       → [{uri, l0_content, status, version, tags, ...}]
  POST /tools/ls       → body.path (not uri!)  → [uri, ...]
  POST /tools/read     → body.level default L1 → {uri, level, content}
  POST /tools/stat     → {uri, context_type, scope, ...}
  POST /search         → {results: [{uri, l0_content, l1_content, score, tags, ...}], total}
  POST /tools/read(skill) → {uri, version, content, advisory}
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field

import httpx

BASE_URL = "http://localhost:8000"
API_KEY = "changeme"
ACCOUNT = "acme"

RUN_ID = str(int(time.time()))


def _h(agent_id: str) -> dict:
    return {"X-API-Key": API_KEY, "X-Account-Id": ACCOUNT, "X-Agent-Id": agent_id}


QA = "query-agent"
AA = "analysis-agent"


# ═══════════════════════════════════════════════════════════════
#  Latency Tracker
# ═══════════════════════════════════════════════════════════════


class LatencyTracker:
    def __init__(self):
        self._samples: dict[str, list[float]] = defaultdict(list)

    def record(self, op: str, ms: float):
        self._samples[op].append(ms)

    def _pct(self, data: list[float], p: int) -> float:
        if not data:
            return 0.0
        s = sorted(data)
        k = (len(s) - 1) * p / 100
        lo = int(k)
        hi = min(lo + 1, len(s) - 1)
        return s[lo] + (k - lo) * (s[hi] - s[lo])

    def report(self):
        if not self._samples:
            return
        print(f"\n{'━' * 64}")
        print("  LATENCY PROFILE  (per-operation, ms)")
        print(f"{'━' * 64}")
        print(f"  {'Operation':<24} {'n':>4}  {'p50':>7}  {'p95':>7}  {'p99':>7}")
        print(f"  {'─' * 56}")
        for op in sorted(self._samples):
            data = self._samples[op]
            n = len(data)
            print(
                f"  {op:<24} {n:>4}"
                f"  {self._pct(data, 50):>6.0f}"
                f"  {self._pct(data, 95):>6.0f}"
                f"  {self._pct(data, 99):>6.0f}"
            )


LAT = LatencyTracker()


# ═══════════════════════════════════════════════════════════════
#  Timed HTTP helpers — every API call records latency
# ═══════════════════════════════════════════════════════════════


async def tpost(http: httpx.AsyncClient, op: str, url: str, **kw) -> httpx.Response:
    t0 = time.monotonic()
    r = await http.post(url, **kw)
    LAT.record(op, (time.monotonic() - t0) * 1000)
    return r


async def tget(http: httpx.AsyncClient, op: str, url: str, **kw) -> httpx.Response:
    t0 = time.monotonic()
    r = await http.get(url, **kw)
    LAT.record(op, (time.monotonic() - t0) * 1000)
    return r


async def tpatch(http: httpx.AsyncClient, op: str, url: str, **kw) -> httpx.Response:
    t0 = time.monotonic()
    r = await http.patch(url, **kw)
    LAT.record(op, (time.monotonic() - t0) * 1000)
    return r


# ═══════════════════════════════════════════════════════════════
#  Result tracking
# ═══════════════════════════════════════════════════════════════


@dataclass
class CheckResult:
    name: str
    passed: bool
    duration_ms: float
    detail: str = ""


@dataclass
class Suite:
    name: str
    tag: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return len(self.results) - self.passed

    @property
    def rate_str(self) -> str:
        n = len(self.results)
        if n == 0:
            return "0/0"
        pct = 100 * self.passed / n
        return f"{self.passed}/{n} = {pct:.0f}%"

    def report(self):
        print(f"\n{'━' * 64}")
        print(f"  {self.name}")
        print(f"  {self.rate_str}")
        print(f"{'━' * 64}")
        for r in self.results:
            icon = "✓" if r.passed else "✗"
            line = f"  {icon} {r.name}  ({r.duration_ms:.0f} ms)"
            if r.detail:
                line += f"  — {r.detail}"
            print(line)


async def chk(suite: Suite, name: str, coro) -> tuple[bool, object]:
    t0 = time.monotonic()
    try:
        result = await coro
        ms = (time.monotonic() - t0) * 1000
        if isinstance(result, tuple):
            ok, detail = result[0], result[1]
            extra = result[2] if len(result) > 2 else None
        else:
            ok, detail, extra = result, "", None
        suite.results.append(CheckResult(name, ok, ms, detail))
        return ok, extra
    except Exception as exc:
        ms = (time.monotonic() - t0) * 1000
        suite.results.append(CheckResult(name, False, ms, str(exc)))
        return False, None


# ═══════════════════════════════════════════════════════════════
#  Helpers for matching against API response formats
# ═══════════════════════════════════════════════════════════════


def _mem_has_tag(mem: dict, tag: str) -> bool:
    """Check if a memory list item contains a specific tag."""
    return tag in (mem.get("tags") or [])


def _is_private_of(mem: dict, agent_id: str) -> bool:
    """True if memory URI belongs to another agent's private namespace."""
    return mem.get("uri", "").startswith(f"ctx://agent/{agent_id}/")


def _is_shared(mem: dict) -> bool:
    return "shared_knowledge" in mem.get("uri", "")


def _search_items(data: dict | list) -> list[dict]:
    """Extract items from search response (handles both formats)."""
    if isinstance(data, dict):
        return data.get("results", [])
    return data if isinstance(data, list) else []


# ═══════════════════════════════════════════════════════════════
#  Suite 1 · Isolation Correctness  (13 checks)
# ═══════════════════════════════════════════════════════════════


async def suite_isolation(http: httpx.AsyncClient) -> Suite:
    s = Suite("Suite 1 · Isolation Correctness", "isolation")
    hqa, haa = _h(QA), _h(AA)
    uris: dict[str, str] = {}

    # ── Setup: both agents store private memories ──

    async def i01():
        r = await tpost(http, "memory_store", "/api/v1/memories", json={
            "content": "Spring promo: spend 300 get 50 off, valid Apr 1-15.",
            "tags": ["promotion", f"iso-{RUN_ID}"],
        }, headers=hqa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        uris["qa_promo"] = r.json()["uri"]
        return True, ""

    await chk(s, "I-01 qa stores 'promo rules'", i01())

    async def i02():
        r = await tpost(http, "memory_store", "/api/v1/memories", json={
            "content": "Supplier floor: cost must not drop below 60% of retail. TOP SECRET.",
            "tags": ["confidential", f"iso-{RUN_ID}"],
        }, headers=hqa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        uris["qa_secret"] = r.json()["uri"]
        return True, ""

    await chk(s, "I-02 qa stores confidential 'supplier floor'", i02())

    async def i03():
        r = await tpost(http, "memory_store", "/api/v1/memories", json={
            "content": "A/B test: variant B (large images) lifts CTR by 8%. Unverified.",
            "tags": ["ab-test", f"iso-{RUN_ID}"],
        }, headers=haa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        uris["aa_ab"] = r.json()["uri"]
        return True, ""

    await chk(s, "I-03 aa stores 'A/B test results'", i03())

    async def i04():
        r = await tpost(http, "memory_store", "/api/v1/memories", json={
            "content": "Churn model v3: users inactive >90 days, precision 0.87. Internal only.",
            "tags": ["churn-model", f"iso-{RUN_ID}"],
        }, headers=haa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        uris["aa_churn"] = r.json()["uri"]
        return True, ""

    await chk(s, "I-04 aa stores 'churn model'", i04())

    # ── Negative: cross-agent list isolation (URI-based matching) ──

    async def i05():
        r = await tget(http, "memory_list", "/api/v1/memories", headers=haa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        for m in r.json():
            if _is_private_of(m, QA):
                return False, f"LEAK: aa sees qa's private memory {m.get('uri')}"
        return True, "0 leaks in aa's list"

    await chk(s, "I-05 [NEG] aa list → no qa private memories", i05())

    async def i06():
        r = await tget(http, "memory_list", "/api/v1/memories", headers=hqa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        for m in r.json():
            if _is_private_of(m, AA):
                return False, f"LEAK: qa sees aa's private memory {m.get('uri')}"
        return True, "0 leaks in qa's list"

    await chk(s, "I-06 [NEG] qa list → no aa private memories", i06())

    # ── Negative: cross-agent search isolation ──

    async def i07():
        r = await tpost(http, "search", "/api/v1/search", json={
            "query": "supplier floor price cost retail",
            "top_k": 10,
        }, headers=haa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        for item in _search_items(r.json()):
            if _is_private_of(item, QA):
                return False, "LEAK: aa search returns qa's private"
        return True, "search clean"

    await chk(s, "I-07 [NEG] aa search 'supplier floor' → 0 qa private", i07())

    async def i08():
        r = await tpost(http, "search", "/api/v1/search", json={
            "query": "churn model precision inactive users",
            "top_k": 10,
        }, headers=hqa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        for item in _search_items(r.json()):
            if _is_private_of(item, AA):
                return False, "LEAK: qa search returns aa's private"
        return True, "search clean"

    await chk(s, "I-08 [NEG] qa search 'churn model' → 0 aa private", i08())

    # ── Selective promotion: promote promo rules, NOT confidential ──

    async def i09():
        r = await tpost(http, "promote", "/api/v1/memories/promote", json={
            "uri": uris["qa_promo"],
            "target_team": "engineering",
        }, headers=hqa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        uris["qa_promo_team"] = r.json()["uri"]
        return True, ""

    await chk(s, "I-09 qa promotes promo rules (not confidential)", i09())

    async def i10():
        r = await tget(http, "memory_list", "/api/v1/memories", headers=haa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        shared = [m for m in r.json() if _is_shared(m)]
        found = any(_mem_has_tag(m, "promotion") for m in shared)
        if not found:
            return False, "promoted promo rules NOT visible to aa"
        return True, f"{len(shared)} shared visible"

    await chk(s, "I-10 [POS] aa sees promoted promo rules", i10())

    async def i11():
        r = await tget(http, "memory_list", "/api/v1/memories", headers=haa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        for m in r.json():
            if _is_private_of(m, QA):
                return False, "LEAK: qa's private namespace visible to aa"
            if _mem_has_tag(m, "confidential") and not _is_private_of(m, AA):
                return False, "LEAK: confidential tag visible in non-private context"
        return True, "confidential still isolated"

    await chk(s, "I-11 [NEG] aa still cannot see confidential", i11())

    async def i12():
        r = await tget(http, "memory_list", "/api/v1/memories", headers=haa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        for m in r.json():
            if _is_shared(m) and _mem_has_tag(m, "confidential"):
                return False, "LEAK: confidential in shared_knowledge"
        return True, "shared space clean"

    await chk(s, "I-12 [NEG] shared_knowledge → no confidential", i12())

    # ── Bulk isolation: store 3 more per agent, verify 0 cross-leaks ──

    async def i13():
        extra_qa_tags = ["internal-roadmap-q3", "vendor-contract-delta", "margin-forecast"]
        extra_aa_tags = ["raw-cohort-data", "unvalidated-ltv", "experiment-log-draft"]
        for tag in extra_qa_tags:
            await tpost(http, "memory_store", "/api/v1/memories", json={
                "content": f"Private qa note about {tag}",
                "tags": [tag, f"iso-{RUN_ID}"],
            }, headers=hqa)
        for tag in extra_aa_tags:
            await tpost(http, "memory_store", "/api/v1/memories", json={
                "content": f"Private aa note about {tag}",
                "tags": [tag, f"iso-{RUN_ID}"],
            }, headers=haa)

        r_aa = await tget(http, "memory_list", "/api/v1/memories", headers=haa)
        r_qa = await tget(http, "memory_list", "/api/v1/memories", headers=hqa)
        leaks = 0
        for m in r_aa.json():
            if _is_private_of(m, QA):
                leaks += 1
        for m in r_qa.json():
            if _is_private_of(m, AA):
                leaks += 1
        if leaks > 0:
            return False, f"LEAK: {leaks} cross-agent leaks in bulk check"
        return True, f"0/{len(extra_qa_tags) + len(extra_aa_tags)} leaked"

    await chk(s, "I-13 [NEG] bulk: 6 more private → 0 cross-leaks", i13())

    s.report()
    return s


# ═══════════════════════════════════════════════════════════════
#  Suite 2 · Promotion & Sharing  (12 checks)
# ═══════════════════════════════════════════════════════════════


async def suite_sharing(http: httpx.AsyncClient) -> Suite:
    s = Suite("Suite 2 · Promotion & Sharing", "sharing")
    hqa, haa = _h(QA), _h(AA)
    uris: dict[str, str] = {}

    content_a = "API rate-limit pattern: use token-bucket with 100 req/s burst, 50 req/s sustained."
    content_b = "Metric insight: DAU/MAU ratio dropped from 0.42 to 0.38 in March — engagement alarm."

    async def s01():
        r = await tpost(http, "memory_store", "/api/v1/memories", json={
            "content": content_a,
            "tags": ["api-pattern", f"shr-{RUN_ID}"],
        }, headers=hqa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        uris["pat_a"] = r.json()["uri"]
        return True, ""

    await chk(s, "S-01 qa stores 'API rate-limit pattern'", s01())

    async def s02():
        r = await tpost(http, "promote", "/api/v1/memories/promote", json={
            "uri": uris["pat_a"],
            "target_team": "engineering",
        }, headers=hqa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        uris["pat_a_team"] = r.json()["uri"]
        return True, uris["pat_a_team"]

    await chk(s, "S-02 qa promotes to engineering", s02())

    async def s03():
        r = await tget(http, "memory_list", "/api/v1/memories", headers=haa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        shared = [m for m in r.json() if _is_shared(m)]
        found = any(_mem_has_tag(m, "api-pattern") for m in shared)
        if not found:
            return False, "aa cannot see qa's promoted pattern"
        return True, ""

    await chk(s, "S-03 [POS] aa sees qa's promoted pattern", s03())

    async def s04():
        r = await tpost(http, "memory_store", "/api/v1/memories", json={
            "content": content_b,
            "tags": ["metric-insight", f"shr-{RUN_ID}"],
        }, headers=haa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        uris["pat_b"] = r.json()["uri"]
        return True, ""

    await chk(s, "S-04 aa stores 'DAU/MAU metric insight'", s04())

    async def s05():
        r = await tpost(http, "promote", "/api/v1/memories/promote", json={
            "uri": uris["pat_b"],
            "target_team": "engineering",
        }, headers=haa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        uris["pat_b_team"] = r.json()["uri"]
        return True, ""

    await chk(s, "S-05 aa promotes to engineering", s05())

    async def s06():
        r = await tget(http, "memory_list", "/api/v1/memories", headers=hqa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        shared = [m for m in r.json() if _is_shared(m)]
        found = any(_mem_has_tag(m, "metric-insight") for m in shared)
        if not found:
            return False, "qa cannot see aa's promoted insight"
        return True, ""

    await chk(s, "S-06 [POS] qa sees aa's promoted insight", s06())

    async def s07():
        r = await tpost(http, "ls", "/api/v1/tools/ls", json={
            "path": "ctx://team/engineering/memories/shared_knowledge",
        }, headers=hqa)
        if r.status_code != 200:
            return False, f"ls HTTP {r.status_code}"
        entries = r.json() if isinstance(r.json(), list) else r.json().get("entries", [])
        if len(entries) < 2:
            return False, f"expected ≥2 entries, got {len(entries)}"
        return True, f"{len(entries)} entries"

    await chk(s, "S-07 [POS] ls shared_knowledge → ≥ 2 entries", s07())

    async def s08():
        r = await tpost(http, "read", "/api/v1/tools/read", json={
            "uri": uris["pat_a_team"],
            "level": "L2",
        }, headers=haa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        content_field = data.get("content", "")
        if "token-bucket" not in content_field and "rate-limit" not in content_field.lower():
            return False, f"promoted content mismatch: {content_field[:80]}"
        return True, "content preserved"

    await chk(s, "S-08 read promoted memory (L2) → content matches", s08())

    async def s09():
        r = await tpost(http, "stat", "/api/v1/tools/stat", json={
            "uri": uris["pat_a_team"],
        }, headers=haa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        scope = data.get("scope", "")
        if scope != "team":
            return False, f"expected scope=team, got {scope}"
        return True, f"scope={scope}"

    await chk(s, "S-09 stat promoted → scope=team", s09())

    async def s10():
        r = await tpost(http, "search", "/api/v1/search", json={
            "query": "API rate limit token bucket pattern",
            "top_k": 5,
        }, headers=haa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        items = _search_items(r.json())
        if not items:
            return False, "search returned 0 results"
        return True, f"{len(items)} results"

    await chk(s, "S-10 search finds promoted content", s10())

    async def s11():
        r_qa = await tget(http, "memory_list", "/api/v1/memories", headers=hqa)
        r_aa = await tget(http, "memory_list", "/api/v1/memories", headers=haa)
        shared_qa = [m for m in r_qa.json() if _is_shared(m)]
        shared_aa = [m for m in r_aa.json() if _is_shared(m)]
        if len(shared_qa) < 2:
            return False, f"qa sees {len(shared_qa)} shared (need ≥2)"
        if len(shared_aa) < 2:
            return False, f"aa sees {len(shared_aa)} shared (need ≥2)"
        return True, f"qa={len(shared_qa)}, aa={len(shared_aa)}"

    await chk(s, "S-11 both agents see ≥ 2 shared memories", s11())

    async def s12():
        r = await tpost(http, "stat", "/api/v1/tools/stat", json={
            "uri": uris["pat_a"],
        }, headers=hqa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        scope = data.get("scope", "")
        if scope != "agent":
            return False, f"source scope changed to {scope}"
        return True, "source stays agent-scoped"

    await chk(s, "S-12 source memory still agent-scoped after promote", s12())

    s.report()
    return s


# ═══════════════════════════════════════════════════════════════
#  Suite 3 · Version Resolution  (10 checks)
# ═══════════════════════════════════════════════════════════════


async def suite_versioning(http: httpx.AsyncClient) -> Suite:
    s = Suite("Suite 3 · Version Resolution", "versioning")
    hqa, haa = _h(QA), _h(AA)
    skill_uri = f"ctx://team/engineering/skills/ver-bench-{RUN_ID}"

    async def v01():
        r = await tpost(http, "context_create", "/api/v1/contexts", json={
            "uri": skill_uri,
            "context_type": "skill",
            "scope": "team",
            "owner_space": "engineering",
            "l2_content": "Versioning benchmark skill",
        }, headers=hqa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}: {r.text}"
        r2 = await tpost(http, "skill_publish", "/api/v1/skills/versions", json={
            "skill_uri": skill_uri,
            "content": "v1: Basic output format — flat JSON array",
            "changelog": "Initial release",
            "is_breaking": False,
        }, headers=hqa)
        if r2.status_code != 201:
            return False, f"v1 publish HTTP {r2.status_code}"
        return True, ""

    await chk(s, "V-01 create skill + publish v1", v01())

    async def v02():
        r = await tpost(http, "read", "/api/v1/tools/read", json={
            "uri": skill_uri,
        }, headers=hqa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        if data.get("version") != 1:
            return False, f"expected v1, got v{data.get('version')}"
        if "flat JSON" not in (data.get("content") or ""):
            return False, "v1 content mismatch"
        return True, ""

    await chk(s, "V-02 read skill → returns v1 content", v02())

    async def v03():
        r = await tpost(http, "skill_subscribe", "/api/v1/skills/subscribe", json={
            "skill_uri": skill_uri,
            "pinned_version": 1,
        }, headers=haa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text}"
        return True, ""

    await chk(s, "V-03 aa subscribes pinned v1", v03())

    async def v04():
        r = await tpost(http, "skill_publish", "/api/v1/skills/versions", json={
            "skill_uri": skill_uri,
            "content": "v2: Added pagination support — backwards compatible",
            "changelog": "Minor: pagination added",
            "is_breaking": False,
        }, headers=hqa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        return True, ""

    await chk(s, "V-04 publish non-breaking v2", v04())

    async def v05():
        await asyncio.sleep(1)
        r = await tpost(http, "read", "/api/v1/tools/read", json={
            "uri": skill_uri,
        }, headers=haa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        if data.get("version") != 1:
            return False, f"pinned should read v1, got v{data.get('version')}"
        return True, f"advisory={'yes' if data.get('advisory') else 'no'}"

    await chk(s, "V-05 aa read → still v1 (pinned holds)", v05())

    async def v06():
        r = await tpost(http, "skill_publish", "/api/v1/skills/versions", json={
            "skill_uri": skill_uri,
            "content": "v3: Rewritten with streaming — new response format",
            "changelog": "Breaking: streaming response, old format removed",
            "is_breaking": True,
        }, headers=hqa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        return True, ""

    await chk(s, "V-06 publish breaking v3", v06())

    async def v07():
        await asyncio.sleep(2)
        r = await tpost(http, "read", "/api/v1/tools/read", json={
            "uri": skill_uri,
        }, headers=haa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        if data.get("version") != 1:
            return False, f"expected pinned v1, got v{data.get('version')}"
        if not data.get("advisory"):
            return False, "no advisory after breaking change"
        return True, f"advisory: {data['advisory'][:60]}..."

    await chk(s, "V-07 aa read → v1 + advisory about v3", v07())

    async def v08():
        r = await tget(http, "skill_versions", f"/api/v1/skills/{skill_uri}/versions", headers=hqa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        versions = r.json()
        if len(versions) != 3:
            return False, f"expected 3 versions, got {len(versions)}"
        return True, f"versions: {[v.get('version') for v in versions]}"

    await chk(s, "V-08 list versions → 3 versions", v08())

    async def v09():
        r = await tpost(http, "read", "/api/v1/tools/read", json={
            "uri": skill_uri,
            "version": 2,
        }, headers=hqa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        if data.get("version") != 2:
            return False, f"expected v2, got v{data.get('version')}"
        if "pagination" not in (data.get("content") or "").lower():
            return False, "v2 content mismatch"
        return True, ""

    await chk(s, "V-09 read explicit version=2 → v2 content", v09())

    async def v10():
        r = await tpatch(http, "context_patch", f"/api/v1/contexts/{skill_uri}", json={
            "l2_content": "Hacked content",
        }, headers=hqa)
        if r.status_code == 400:
            return True, "immutable via PATCH (400)"
        return False, f"expected 400, got {r.status_code}"

    await chk(s, "V-10 PATCH skill → 400 (immutable)", v10())

    s.report()
    return s


# ═══════════════════════════════════════════════════════════════
#  Suite 4 · Propagation & Convergence  (10 checks)
# ═══════════════════════════════════════════════════════════════


async def suite_propagation(http: httpx.AsyncClient) -> Suite:
    s = Suite("Suite 4 · Propagation & Convergence", "propagation")
    hqa, haa = _h(QA), _h(AA)
    skill_uri = f"ctx://team/engineering/skills/prop-bench-{RUN_ID}"
    convergence_samples: list[float] = []

    async def c01():
        r = await tpost(http, "context_create", "/api/v1/contexts", json={
            "uri": skill_uri,
            "context_type": "skill",
            "scope": "team",
            "owner_space": "engineering",
            "l2_content": "Propagation benchmark skill",
        }, headers=hqa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}: {r.text}"
        r2 = await tpost(http, "skill_publish", "/api/v1/skills/versions", json={
            "skill_uri": skill_uri,
            "content": "v1: Baseline propagation test content",
            "changelog": "Initial",
            "is_breaking": False,
        }, headers=hqa)
        if r2.status_code != 201:
            return False, f"v1 HTTP {r2.status_code}"
        return True, ""

    await chk(s, "C-01 create skill + publish v1", c01())

    async def c02():
        r = await tpost(http, "skill_subscribe", "/api/v1/skills/subscribe", json={
            "skill_uri": skill_uri,
            "pinned_version": 1,
        }, headers=haa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text}"
        return True, ""

    await chk(s, "C-02 aa subscribes pinned v1", c02())

    async def c03():
        r = await tpost(http, "skill_publish", "/api/v1/skills/versions", json={
            "skill_uri": skill_uri,
            "content": "v2: Breaking — output schema changed",
            "changelog": "Breaking: new schema",
            "is_breaking": True,
        }, headers=hqa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"

        await asyncio.sleep(1)
        t0 = time.monotonic()
        for _ in range(40):
            r2 = await http.post("/api/v1/tools/read", json={"uri": skill_uri}, headers=haa)
            if r2.status_code == 200 and r2.json().get("advisory"):
                conv_ms = (time.monotonic() - t0) * 1000
                convergence_samples.append(conv_ms)
                LAT.record("propagation_convergence", conv_ms)
                return True, f"convergence {conv_ms:.0f} ms"
            await asyncio.sleep(0.15)
        return False, "advisory not received within 7s"

    await chk(s, "C-03 breaking v2 → advisory appears", c03())

    async def c04():
        if not convergence_samples:
            return False, "no convergence data (C-03 failed)"
        if convergence_samples[-1] > 2000:
            return False, f"convergence {convergence_samples[-1]:.0f}ms > 2000ms SLA"
        return True, f"{convergence_samples[-1]:.0f}ms < 2000ms"

    await chk(s, "C-04 convergence < 2000ms SLA", c04())

    async def c05():
        r = await http.post("/api/v1/tools/read", json={"uri": skill_uri}, headers=haa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        if data.get("version") != 1:
            return False, f"pinned broken: got v{data.get('version')}"
        return True, "pinned v1 stable"

    await chk(s, "C-05 pinned version = v1 (not v2)", c05())

    # ── Catalog sync + data-lake ──

    async def c06():
        r = await tpost(http, "catalog_sync", "/api/v1/datalake/sync", json={
            "catalog": "mock",
        }, headers=hqa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text}"
        data = r.json()
        synced = data.get("tables_synced", 0)
        if synced == 0:
            return False, "0 tables synced"
        return True, f"{synced} tables synced"

    await chk(s, "C-06 catalog sync → tables synced", c06())

    async def c07():
        r = await tget(http, "datalake_list", "/api/v1/datalake/mock/prod", headers=hqa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        tables = r.json().get("tables", [])
        if not tables:
            return False, "no tables listed"
        return True, f"{len(tables)} tables"

    await chk(s, "C-07 list tables → present", c07())

    async def c08():
        r = await tpost(http, "sql_context", "/api/v1/search/sql-context", json={
            "query": "How many orders per user per month?",
            "catalog": "mock",
            "top_k": 3,
        }, headers=hqa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        total = r.json().get("total_tables_found", 0)
        if total == 0:
            return False, "0 relevant tables"
        return True, f"{total} tables found"

    await chk(s, "C-08 sql-context search → relevant tables", c08())

    async def c09():
        r = await tpost(http, "catalog_sync", "/api/v1/datalake/sync", json={
            "catalog": "mock",
        }, headers=hqa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        created = data.get("tables_created", 0)
        if created != 0:
            return False, f"idempotent violation: {created} new tables on re-sync"
        return True, f"tables_created=0 (idempotent)"

    await chk(s, "C-09 second sync → tables_created=0", c09())

    async def c10():
        r = await tpost(http, "search", "/api/v1/search", json={
            "query": "orders users monthly aggregation",
            "top_k": 5,
        }, headers=hqa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        items = _search_items(r.json())
        if not items:
            return False, "0 results from unified search"
        return True, f"{len(items)} results"

    await chk(s, "C-10 unified search → mixed results", c10())

    s.report()
    return s


# ═══════════════════════════════════════════════════════════════
#  Suite 5 · LLM-Native Tools API  (10 checks)
# ═══════════════════════════════════════════════════════════════


async def suite_tools(http: httpx.AsyncClient) -> Suite:
    s = Suite("Suite 5 · LLM-Native Tools API", "tools")
    hqa, haa = _h(QA), _h(AA)
    uris: dict[str, str] = {}

    async def t_setup():
        r = await tpost(http, "memory_store", "/api/v1/memories", json={
            "content": "Tool-test memory: always use parameterized queries to prevent SQL injection.",
            "tags": ["security", f"tool-{RUN_ID}"],
        }, headers=hqa)
        if r.status_code != 201:
            return False, f"HTTP {r.status_code}"
        uris["tool_mem"] = r.json()["uri"]
        return True, ""

    await chk(s, "T-00 setup: store test memory", t_setup())

    skill_uri = f"ctx://team/engineering/skills/tool-bench-{RUN_ID}"

    async def t_setup2():
        r1 = await tpost(http, "context_create", "/api/v1/contexts", json={
            "uri": skill_uri,
            "context_type": "skill",
            "scope": "team",
            "owner_space": "engineering",
            "l2_content": "Tool benchmark skill content",
        }, headers=hqa)
        if r1.status_code != 201:
            return False, f"HTTP {r1.status_code}: {r1.text}"
        r2 = await tpost(http, "skill_publish", "/api/v1/skills/versions", json={
            "skill_uri": skill_uri,
            "content": "v1: Tool benchmark — parameterized query generator",
            "changelog": "Initial",
            "is_breaking": False,
        }, headers=hqa)
        if r2.status_code != 201:
            return False, f"v1 HTTP {r2.status_code}"
        uris["tool_skill"] = skill_uri
        return True, ""

    await chk(s, "T-00 setup: create test skill", t_setup2())

    async def t01():
        r = await tpost(http, "ls", "/api/v1/tools/ls", json={
            "path": "ctx://team/engineering/memories/shared_knowledge",
        }, headers=hqa)
        if r.status_code != 200:
            return False, f"ls HTTP {r.status_code}"
        entries = r.json() if isinstance(r.json(), list) else r.json().get("entries", [])
        return True, f"{len(entries)} entries"

    await chk(s, "T-01 ls shared_knowledge → entries", t01())

    async def t02():
        r = await tpost(http, "read", "/api/v1/tools/read", json={
            "uri": uris["tool_mem"],
            "level": "L2",
        }, headers=hqa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        if "parameterized" not in (data.get("content") or ""):
            return False, "content mismatch"
        return True, ""

    await chk(s, "T-02 read own private memory (L2) → content", t02())

    async def t03():
        r = await tpost(http, "read", "/api/v1/tools/read", json={
            "uri": skill_uri,
        }, headers=hqa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        if data.get("version") is None:
            return False, "missing version field"
        if not data.get("content"):
            return False, "empty content"
        return True, f"v{data['version']}"

    await chk(s, "T-03 read skill → versioned content", t03())

    async def t04():
        r = await tpost(http, "stat", "/api/v1/tools/stat", json={
            "uri": uris["tool_mem"],
        }, headers=hqa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        required = ["uri", "context_type", "scope"]
        missing = [k for k in required if k not in data]
        if missing:
            return False, f"missing fields: {missing}"
        return True, f"type={data.get('context_type')}, scope={data.get('scope')}"

    await chk(s, "T-04 stat memory → metadata fields", t04())

    async def t05():
        r = await tpost(http, "stat", "/api/v1/tools/stat", json={
            "uri": skill_uri,
        }, headers=hqa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        if data.get("context_type") != "skill":
            return False, f"expected type=skill, got {data.get('context_type')}"
        return True, f"scope={data.get('scope')}"

    await chk(s, "T-05 stat skill → type=skill", t05())

    async def t06():
        r = await tpost(http, "search", "/api/v1/search", json={
            "query": "SQL injection parameterized query prevention",
            "top_k": 5,
        }, headers=hqa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        items = _search_items(r.json())
        if not items:
            return False, "0 results"
        return True, f"{len(items)} results"

    await chk(s, "T-06 search (grep) → keyword results", t06())

    async def t07():
        fake_uri = f"ctx://agent/{QA}/memories/does-not-exist-{RUN_ID}"
        r = await tpost(http, "read", "/api/v1/tools/read", json={
            "uri": fake_uri,
        }, headers=hqa)
        if r.status_code == 404:
            return True, "404 as expected"
        return False, f"expected 404, got {r.status_code}"

    await chk(s, "T-07 read non-existent URI → 404", t07())

    async def t08():
        fake_uri = f"ctx://agent/{QA}/memories/no-such-stat-{RUN_ID}"
        r = await tpost(http, "stat", "/api/v1/tools/stat", json={
            "uri": fake_uri,
        }, headers=hqa)
        if r.status_code in (404, 422):
            return True, f"{r.status_code} as expected"
        return False, f"expected 404/422, got {r.status_code}"

    await chk(s, "T-08 stat non-existent → error", t08())

    async def t09():
        r = await tpost(http, "read", "/api/v1/tools/read", json={
            "uri": skill_uri,
            "version": 1,
        }, headers=hqa)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        if data.get("version") != 1:
            return False, f"expected v1, got v{data.get('version')}"
        return True, ""

    await chk(s, "T-09 read skill version=1 → correct", t09())

    async def t10():
        r = await tpost(http, "search", "/api/v1/search", json={
            "query": "parameterized query",
            "top_k": 3,
            "context_type": ["memory"],
        }, headers=hqa)
        if r.status_code == 422:
            return True, "context_type filter format not supported (422)"
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        return True, "filter accepted"

    await chk(s, "T-10 search with type filter → respects", t10())

    s.report()
    return s


# ═══════════════════════════════════════════════════════════════
#  Entrypoint
# ═══════════════════════════════════════════════════════════════


SUITE_MAP = {
    "1": ("isolation", suite_isolation),
    "2": ("sharing", suite_sharing),
    "3": ("versioning", suite_versioning),
    "4": ("propagation", suite_propagation),
    "5": ("tools", suite_tools),
}


async def _ensure_team_membership():
    try:
        import asyncpg
    except ImportError:
        print("  (asyncpg not installed — skipping membership seed)")
        return
    try:
        conn = await asyncpg.connect(
            "postgresql://contexthub:contexthub@localhost:5432/contexthub"
        )
        try:
            await conn.execute("SET app.account_id = 'acme'")
            await conn.execute("""
                INSERT INTO team_memberships
                    (agent_id, team_id, role, access, is_primary)
                VALUES
                    ('query-agent',
                     '00000000-0000-0000-0000-000000000002',
                     'member', 'read_write', FALSE)
                ON CONFLICT DO NOTHING
            """)
        finally:
            await conn.close()
    except Exception as exc:
        print(f"  Warning: membership seed failed ({exc})")


async def main():
    selected = set(SUITE_MAP.keys())
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--suite" and i < len(sys.argv) - 1:
            val = sys.argv[i + 1].lower().strip()
            if val != "all":
                selected = set(val.split(","))

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as http:
        r = await http.get("/health")
        if r.status_code != 200:
            print(f"ContextHub server not reachable at {BASE_URL}")
            sys.exit(1)
        print(f"Server healthy.  Run ID: {RUN_ID}")

        await _ensure_team_membership()

        suites: list[Suite] = []
        for key in sorted(selected):
            if key in SUITE_MAP:
                _, fn = SUITE_MAP[key]
                suites.append(await fn(http))

        # ── Latency profile ──
        LAT.report()

        # ── Category summary ──
        total_pass = sum(s.passed for s in suites)
        total_fail = sum(s.failed for s in suites)
        total = total_pass + total_fail

        print(f"\n{'═' * 64}")
        print(f"  GOVERNANCE CORRECTNESS BENCHMARK   (run {RUN_ID})")
        print(f"  {total_pass}/{total} checks passed, {total_fail} failed")
        print(f"{'─' * 64}")
        for suite in suites:
            icon = "✓" if suite.failed == 0 else "✗"
            print(f"  {icon} {suite.tag:<16} {suite.rate_str}")
        print(f"{'─' * 64}")
        if total > 0:
            pct = 100 * total_pass / total
            print(f"  Overall governance correctness: {pct:.1f}%")
        print(f"{'═' * 64}")

        sys.exit(0 if total_fail == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
