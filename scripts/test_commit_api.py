"""API smoke test for AMC commit endpoint only."""

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
    raw = _load_json(Path(args.trajectory_file))
    # Support both old format (top-level list) and new format ({ "query": ..., "trajectory": [...] }).
    if isinstance(raw, list):
        commit_steps = raw
    elif isinstance(raw, dict) and isinstance(raw.get("trajectory"), list):
        commit_steps = raw["trajectory"]
    else:
        raise ValueError("trajectory file must be a JSON array or an object with a 'trajectory' list field")

    base = args.base_url.rstrip("/")
    commit_url = f"{base}/commit"

    resolved_account_id = str(args.account_id or "account-local").strip()
    commit_payload = {
        "session_id": args.session_id,
        "task_id": args.task_id,
        "trajectory": commit_steps,
        "labels": {},
        "is_incremental": False,
        "visualize_graph_png": False,
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
                commit_url,
                json=commit_payload,
                headers=headers,
                timeout=float(args.commit_timeout),
            )
        except httpx.ReadTimeout as e:
            raise RuntimeError(
                f"commit timed out after {float(args.commit_timeout):.1f}s (url={commit_url})"
            ) from e
        commit_ms = (time.perf_counter() - t1) * 1000.0

    if resp.status_code >= 400:
        raise RuntimeError(f"commit failed [{resp.status_code}] {commit_url}: {resp.text}")
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("commit returned non-object JSON")
    if not str(data.get("trajectory_id") or "").strip():
        raise RuntimeError("commit response missing trajectory_id")
    if int(data.get("nodes") or 0) <= 0:
        raise RuntimeError("commit response nodes should be > 0")
    edge_count = int(data.get("edges") or 0)
    if edge_count < int(args.min_edges):
        raise RuntimeError(f"commit response edges should be >= {int(args.min_edges)}")

    out = {
        "summary": {
            "health_url": args.health_url,
            "commit_url": commit_url,
            "trajectory_id": data.get("trajectory_id"),
            "status": data.get("status"),
            "nodes": data.get("nodes"),
            "edges": data.get("edges"),
            "timing_ms": {
                "health": round(health_ms, 2),
                "commit": round(commit_ms, 2),
            },
        },
        "commit": data,
    }
    if args.pretty:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"summary": out["summary"]}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Smoke test AMC commit FastAPI endpoint")
    p.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1/amc", help="AMC API base URL")
    p.add_argument("--health-url", default="http://127.0.0.1:8000/healthz", help="Health endpoint URL")
    p.add_argument("--account-id", default="account-local")
    p.add_argument("--agent-id", default="agent-local")
    p.add_argument("--session-id", default="session-local")
    p.add_argument("--task-id", default="task-api-smoke")
    p.add_argument("--trajectory-file", default="sample_traj/traj1.json")
    p.add_argument("--health-timeout", type=float, default=15.0, help="Health check timeout (seconds)")
    p.add_argument("--commit-timeout", type=float, default=600.0, help="Commit request timeout (seconds)")
    p.add_argument(
        "--min-edges",
        type=int,
        default=0,
        help="Minimum expected edge count in commit response (default: 0)",
    )
    p.add_argument("--pretty", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args)
    except Exception as e:
        print(f"[test-commit-api] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
