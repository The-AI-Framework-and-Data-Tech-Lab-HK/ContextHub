"""Recompute MEME-aligned per-episode cost from existing summary.json files.

No rerun needed: the token buckets in summary.json already carry real API
usage. This re-derives the per-episode token & USD figures (two scopes:
meme_aligned = ingest+answer, full = +oracle) so paper numbers can be filled
in from committed runs.

Usage:
    python3 -m integrations.memebench.recompute_cost runs/rawB_hop1_planA [...]
    python3 -m integrations.memebench.recompute_cost            # all runs/*
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from integrations.memebench.metrics import cost_per_episode

RUNS_DIR = Path(__file__).parent / "runs"


def _report(summary_path: Path) -> None:
    s = json.loads(summary_path.read_text(encoding="utf-8"))
    cost = s.get("cost", {})
    n_ok = s.get("n_ok", 0)
    buckets = {k: cost[k] for k in
               ("extract_llm", "ingest_llm", "inference_llm", "oracle_llm", "judge_llm")
               if isinstance(cost.get(k), dict)}
    pe = cost_per_episode(buckets, n_ok)
    a, f = pe["meme_aligned"], pe["full"]
    real = all(b.get("tokens_are_real") for b in buckets.values() if b)
    print(f"\n{summary_path.parent.name}  (model={s.get('model')}, "
          f"edge_mode={s.get('edge_mode')}, n_ok={n_ok}, real_usage={real})")
    print(f"  MEME-aligned (ingest+answer): "
          f"{a['tokens_per_episode']:,.0f} tok/ep, ${a['usd_per_episode']:.5f}/ep")
    print(f"  full (+oracle):               "
          f"{f['tokens_per_episode']:,.0f} tok/ep, ${f['usd_per_episode']:.5f}/ep")


def main(argv: list[str]) -> None:
    if argv:
        paths = [Path(a) if Path(a).name == "summary.json" else Path(a) / "summary.json"
                 for a in argv]
    else:
        paths = sorted(RUNS_DIR.glob("*/summary.json"))
    for p in paths:
        if p.exists():
            _report(p)
        else:
            print(f"skip (no summary.json): {p}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
