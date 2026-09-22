"""Post-run cost audit: does every episode's paid work actually reach a cost bucket?

Guards the historical defect where a resumed run under-counted spend by two
orders of magnitude: imported artifacts never entered the cost buckets, so the
run looked cheap while the money had been spent. The tell is
`inference_calls < n_ok` -- fewer recorded inference calls than successful
episodes is impossible when every episode makes at least one.

"Finished without an error" is not the check. This is.
"""

from __future__ import annotations

import argparse
import glob
import json
from collections import Counter
from pathlib import Path


def audit(run_dir: Path) -> dict:
    summary_path = run_dir / "run_cost_summary.json"
    if not summary_path.is_file():
        raise SystemExit(f"{run_dir.name}: run_cost_summary.json missing (tier unfinished)")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    n_ok = int(summary["episode_count"])

    calls = Counter()
    usd = 0.0
    ledger_rows = 0
    episodes = 0
    for path in sorted(glob.glob(str(run_dir / "artifacts" / "full-run" / "*" / "artifact.json"))):
        artifact = json.loads(Path(path).read_text(encoding="utf-8"))
        episodes += 1
        cost = artifact["cost"]
        ledger_rows += len(cost.get("calls") or [])
        for bucket, layer in (cost["paid_execution"]["layers"] or {}).items():
            calls[bucket] += int(layer.get("calls") or 0)
            usd += float(layer.get("known_usd") or 0.0)

    inference_calls = calls["inference_llm"]
    return {
        "tier": run_dir.name,
        "n_ok": n_ok,
        "episodes_on_disk": episodes,
        "inference_calls": inference_calls,
        "calls_by_bucket": dict(sorted(calls.items())),
        "ledger_rows": ledger_rows,
        "paid_execution_usd": usd,
        "summary_total_usd": float(summary["total_usd"]),
        # An episode answers three arms, so one call per arm is the floor.
        "pass_inference_ge_n_ok": inference_calls >= n_ok,
        "pass_episode_count_matches_disk": episodes == n_ok,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, nargs="+")
    args = parser.parse_args()
    failed = False
    for run_dir in args.run_dir:
        row = audit(run_dir)
        ok = row["pass_inference_ge_n_ok"] and row["pass_episode_count_matches_disk"]
        failed = failed or not ok
        print(f"\n### {row['tier']}   {'OK' if ok else 'FAILED'}")
        print(f"  n_ok={row['n_ok']}  episodes_on_disk={row['episodes_on_disk']}")
        print(f"  inference_calls={row['inference_calls']}  (must be >= n_ok)")
        print(f"  calls by bucket: {row['calls_by_bucket']}")
        print(f"  ledger rows: {row['ledger_rows']}")
        print(
            f"  paid_execution sum ${row['paid_execution_usd']:.4f}"
            f"   summary total ${row['summary_total_usd']:.4f}"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
