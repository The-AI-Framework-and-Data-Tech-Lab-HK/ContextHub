"""API smoke test for AMC promote endpoint only."""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx


def run(args: argparse.Namespace) -> int:
    if args.tenant_id:
        print("[AMC] --tenant-id is deprecated; use --account-id.")
    resolved_account_id = str(args.account_id or args.tenant_id or "account-local").strip()
    if not str(args.trajectory_id or "").strip():
        raise ValueError("trajectory_id is required")

    base = args.base_url.rstrip("/")
    promote_url = f"{base}/promote"
    payload = {
        "trajectory_id": str(args.trajectory_id),
        "target_team": str(args.target_team),
        "reason": str(args.reason or ""),
    }
    headers = {
        "X-Account-Id": resolved_account_id,
        "X-Agent-Id": str(args.agent_id),
    }

    with httpx.Client() as client:
        t0 = time.perf_counter()
        health = client.get(args.health_url, timeout=float(args.health_timeout))
        if health.status_code >= 400:
            raise RuntimeError(f"health check failed [{health.status_code}]: {health.text}")
        health_ms = (time.perf_counter() - t0) * 1000.0

        t1 = time.perf_counter()
        try:
            resp = client.post(
                promote_url,
                json=payload,
                headers=headers,
                timeout=float(args.promote_timeout),
            )
        except httpx.ReadTimeout as e:
            raise RuntimeError(
                f"promote timed out after {float(args.promote_timeout):.1f}s (url={promote_url})"
            ) from e
        promote_ms = (time.perf_counter() - t1) * 1000.0

    if resp.status_code >= 400:
        raise RuntimeError(f"promote failed [{resp.status_code}] {promote_url}: {resp.text}")
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("promote returned non-object JSON")
    for k in ("source_uri", "target_uri", "trajectory_id", "scope", "owner_space"):
        if not str(data.get(k) or "").strip():
            raise RuntimeError(f"promote response missing {k}")

    out = {
        "summary": {
            "health_url": args.health_url,
            "promote_url": promote_url,
            "trajectory_id": data.get("trajectory_id"),
            "source_uri": data.get("source_uri"),
            "target_uri": data.get("target_uri"),
            "status": data.get("status"),
            "vector_upserted_docs": ((data.get("vector_index_summary") or {}).get("upserted_docs")),
            "timing_ms": {
                "health": round(health_ms, 2),
                "promote": round(promote_ms, 2),
            },
        },
        "promote": data,
    }
    if args.pretty:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"summary": out["summary"]}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Smoke test AMC promote FastAPI endpoint")
    p.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1/amc", help="AMC API base URL")
    p.add_argument("--health-url", default="http://127.0.0.1:8000/healthz", help="Health endpoint URL")
    p.add_argument("--account-id", default="account-local")
    p.add_argument("--tenant-id", default=None, help="Deprecated alias of account_id")
    p.add_argument("--agent-id", default="agent-local")
    p.add_argument("--trajectory-id", required=True)
    p.add_argument("--target-team", default="engineering")
    p.add_argument("--reason", default="promote reusable workflow")
    p.add_argument("--health-timeout", type=float, default=15.0, help="Health check timeout (seconds)")
    p.add_argument("--promote-timeout", type=float, default=120.0, help="Promote request timeout (seconds)")
    p.add_argument("--pretty", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args)
    except Exception as e:
        print(f"[test-promote-api] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
