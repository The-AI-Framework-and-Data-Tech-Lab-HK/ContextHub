"""API smoke test for AMC retrieve endpoint only."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx


def _load_json(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def run(args: argparse.Namespace) -> int:
    partial_steps = None
    if args.partial_trajectory_file:
        partial_steps = _load_json(Path(args.partial_trajectory_file))
        if not isinstance(partial_steps, list):
            raise ValueError("partial trajectory file must be a JSON array")

    base = args.base_url.rstrip("/")
    retrieve_url = f"{base}/retrieve"
    retrieve_payload = {
        "tenant_id": args.tenant_id,
        "agent_id": args.agent_id,
        "query": {
            "task_description": args.task_description,
            "partial_trajectory": partial_steps,
            "constraints": {"tool_whitelist": args.tool_whitelist},
            "task_type": args.task_type,
        },
        "top_k": args.top_k,
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
                retrieve_url,
                json=retrieve_payload,
                timeout=float(args.retrieve_timeout),
            )
        except httpx.ReadTimeout as e:
            raise RuntimeError(
                f"retrieve timed out after {float(args.retrieve_timeout):.1f}s (url={retrieve_url})"
            ) from e
        retrieve_ms = (time.perf_counter() - t1) * 1000.0

    if resp.status_code >= 400:
        raise RuntimeError(f"retrieve failed [{resp.status_code}] {retrieve_url}: {resp.text}")
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("retrieve returned non-object JSON")
    items = data.get("items")
    if not isinstance(items, list):
        raise RuntimeError("retrieve response missing items list")

    out = {
        "summary": {
            "health_url": args.health_url,
            "retrieve_url": retrieve_url,
            "top_k": args.top_k,
            "retrieve_hit_count": len(items),
            "top_hit": (items[0].get("trajectory_id") if items else None),
            "timing_ms": {
                "health": round(health_ms, 2),
                "retrieve": round(retrieve_ms, 2),
            },
        },
        "retrieve": data,
    }
    if args.pretty:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"summary": out["summary"]}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Smoke test AMC retrieve FastAPI endpoint")
    p.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1/amc", help="AMC API base URL")
    p.add_argument("--health-url", default="http://127.0.0.1:8000/healthz", help="Health endpoint URL")
    p.add_argument("--tenant-id", default="tenant-local")
    p.add_argument("--agent-id", default="agent-local")
    p.add_argument("--task-type", default="sql_analysis")
    p.add_argument("--task-description", default="中小微 企业信贷及经营数据")
    p.add_argument("--partial-trajectory-file", default="sample_graph_query/pq02_retry_pattern_traj1.json")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--tool-whitelist", nargs="*", default=["local_db_sql"], help="Space-separated tool list")
    p.add_argument("--health-timeout", type=float, default=15.0, help="Health check timeout (seconds)")
    p.add_argument("--retrieve-timeout", type=float, default=600.0, help="Retrieve request timeout (seconds)")
    p.add_argument("--pretty", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args)
    except Exception as e:
        print(f"[test-retrieve-api] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
