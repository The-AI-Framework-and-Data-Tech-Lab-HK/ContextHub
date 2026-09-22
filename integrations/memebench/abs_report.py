"""Stratified reporting for the `Abs` evaluation (plan step 4, block 8).

Reads full-run artifacts and prints the three gold criteria *separately* rather
than only their conjunction. That split is the point: stages 1-2 (abstain, cite
the old value) measure whether propagation did its job, while stage 3 (name the
upstream) is limited by the frozen v3 graph carrying no entity labels. Reporting
only `all_three` would let the second mask the first.

hop2 is additionally split into "root reachable under the strict alias reading"
(21) and "target extracted but the v3 graph lacks the edge" (8). The 8 are
expected to fail and are kept in the denominator on purpose -- they are a P1
recall hole, not a dataset defect -- so a flat hop2 number would read as a
propagation failure that did not happen.
"""

from __future__ import annotations

import argparse
import glob
import json
from collections import Counter
from pathlib import Path

# The 8 hop2 episodes whose dependency chain is real in the dataset's own fact
# fields but which the frozen v3 graph never discovered. See the plan's
# "why these 8 stay in the denominator" note.
HOP2_MISSING_EDGE = frozenset(
    {"sw_001", "sw_009", "sw_010", "sw_019", "sw_028", "sw_036", "sw_044", "sw_046"}
)
STAGES = ("abstained", "cited_prev", "named_upstream")


def load_artifacts(run_dir: Path) -> list[dict]:
    rows = []
    for path in sorted(glob.glob(str(run_dir / "artifacts" / "full-run" / "*" / "artifact.json"))):
        rows.append(json.loads(Path(path).read_text(encoding="utf-8")))
    return rows


def summarize(artifacts: list[dict], *, arm: str) -> dict:
    """Per-stage hit counts for one arm, plus the conjunction."""
    counts = Counter()
    total = 0
    for artifact in artifacts:
        stage = ((artifact.get("judge") or {}).get("abs_scoring") or {}).get("stages") or {}
        row = stage.get(arm)
        if not isinstance(row, dict):
            continue
        total += 1
        for name in STAGES:
            if row.get(name) is True:
                counts[name] += 1
        if row.get("correct") is True:
            counts["all_three"] += 1
    return {"n": total, **{k: counts[k] for k in (*STAGES, "all_three")}}


def pct(part: int, whole: int) -> str:
    return f"{part:3d}/{whole:<3d} ({part / whole * 100:5.1f}%)" if whole else "   n/a"


def print_block(label: str, artifacts: list[dict]) -> None:
    if not artifacts:
        print(f"\n{label}: (no cases)")
        return
    print(f"\n{label}  n={len(artifacts)}")
    print(f"  {'arm':6s} {'abstain':>16s} {'cite old':>16s} {'name upstream':>16s} {'all three':>16s}")
    for arm in ("off", "on"):
        s = summarize(artifacts, arm=arm)
        print(
            f"  {arm:6s} "
            f"{pct(s['abstained'], s['n']):>16s} "
            f"{pct(s['cited_prev'], s['n']):>16s} "
            f"{pct(s['named_upstream'], s['n']):>16s} "
            f"{pct(s['all_three'], s['n']):>16s}"
        )
    before = summarize(artifacts, arm="before")
    before_ok = sum(
        1
        for a in artifacts
        if (((a.get("judge") or {}).get("abs_scoring") or {}).get("stages") or {})
        .get("before", {})
        .get("correct")
        is True
    )
    print(f"  before (plain old value, containment): {pct(before_ok, before['n'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, nargs="+")
    args = parser.parse_args()

    for run_dir in args.run_dir:
        artifacts = load_artifacts(run_dir)
        if not artifacts:
            print(f"\n### {run_dir.name}: no full-run artifacts")
            continue
        hop = artifacts[0].get("evaluation_hop", 1)
        task_type = artifacts[0].get("task_type", "?")
        print(f"\n{'=' * 78}\n### {run_dir.name}   task_type={task_type} hop={hop}\n{'=' * 78}")
        if task_type != "Abs":
            print(f"  (not an Abs tier; {len(artifacts)} artifacts)")
            continue

        print_block("ALL", artifacts)
        if hop == 2:
            reachable = [a for a in artifacts if a["episode_id"] not in HOP2_MISSING_EDGE]
            missing = [a for a in artifacts if a["episode_id"] in HOP2_MISSING_EDGE]
            print_block("hop2 / root reachable (strict alias)", reachable)
            print_block("hop2 / v3 graph lacks the edge (expected to fail)", missing)
        for domain in ("pl", "sw"):
            print_block(
                f"domain {domain}",
                [a for a in artifacts if a["episode_id"].startswith(domain)],
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
