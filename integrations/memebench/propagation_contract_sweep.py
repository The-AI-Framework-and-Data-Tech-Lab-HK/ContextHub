"""Fill the propagation contract with measured numbers and sweep the one number
that has no calibrated source.

Why this exists
---------------
``run_full100_v3_p2.DIRECT_CONTRACT`` carries placeholder values: every judging
tier has ``delta=1.0`` and the costs are ordinals (0/1/1/1/2).  With delta=1.0 no
judging tier is ever feasible, so the planner can only ever return
all-direct-stale regardless of the risk budget.  That degeneracy is an artifact
of the placeholder, not a result.

This module builds the contract from measured artifacts instead:

* ``delta``  <- ``calibration_bootstrap.json``'s ``delta_reported``, which is
  ``max(independent Clopper-Pearson, episode-cluster bootstrap)`` per option per
  hop.  ``direct-stale`` is 0 by construction (it never says fresh).
* ``expected_cost`` <- ``propagation_planner_eval.calibrate_contracts``, which
  already implements the right cost model: observed judge-bundle tokens for the
  tier, plus ``recompute_cost`` charged on every edge that tier marks stale.

The third number -- what one wrongly-withheld node costs, expressed in tokens --
has no calibrated source and is not measurable from this data.  The proposal
(S4.4) says a deployer supplies it or the components are reported separately, so
this module does NOT pick one: it sweeps ``chronological_policy``'s existing
``RECOMPUTE_COST_SENSITIVITY`` and reports how the chosen tier moves.

Interpreting the output
-----------------------
``per_length`` is the primary result: for a uniform path of length L, which tier
is cheapest among those satisfying ``L * delta <= eps_prop``.  This is exact for
a global contract, because every edge sees the same menu, so the only thing that
distinguishes edges is the length of the path they sit on.

``planner`` is a secondary check that runs the real solver on real graph shapes.
Its edges come from the CALIBRATION edge set (candidate judge edges, mostly
negative), NOT from the frozen P1 v3 graph the e2e runner propagates over.  Read
it as "the solver agrees with per_length on realistic shapes", not as a
prediction of the e2e mode mix.

Contamination (read before quoting any number)
----------------------------------------------
delta is calibrated on the SAME episodes it is later applied to -- hop1 100/100,
hop2 64/64, complete overlap.  ``evaluation_episodes_in_calibration=True`` is
passed to the planner so ``certification_blocked_reason`` records this and
``certified`` stays false.  A held-out re-split was deliberately NOT done
(2026-09-08 decision): the analyst has already seen all 100 episodes across weeks
of prior analysis, so a split now would produce something that looks like
held-out validation without being it.  These are development-set numbers.

Zero API.  Reads only runs/{neg_edge_set,judge_routing}_hop{1,2}.json and
runs/calibration_bootstrap_20260903/calibration_bootstrap.json.

Usage:
    .venv/bin/python -m integrations.memebench.propagation_contract_sweep \
        --runs-dir integrations/memebench/runs \
        --bootstrap integrations/memebench/runs/calibration_bootstrap_20260903/calibration_bootstrap.json \
        --eps-prop 0.2 \
        --recompute-costs 0 100 1000 10000 \
        --alpha 0.05 \
        --out integrations/memebench/runs/<dir>/contract_sweep.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .planned_propagation import PLAN_MODES, plan_published_graph
from .propagation_planner_eval import calibrate_contracts, load_joined

HOPS = (1, 2)
STOCHASTIC = ("J1", "J3", "J4", "cascade")
MAX_PATH_LENGTH = 3


def sha256_16(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def build_contract(
    rows: list[dict[str, Any]],
    bootstrap_options: dict[str, Any],
    *,
    recompute_cost: float,
    alpha: float,
) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    """Measured cost from calibrate_contracts, measured delta from the bootstrap.

    calibrate_contracts also produces its own independent-CP delta.  We keep the
    bootstrap's ``delta_reported`` instead because it is the larger of the two by
    construction, and record both so the substitution stays auditable.
    """
    contracts, source = calibrate_contracts(
        rows,
        method="cp-upper",
        alpha=alpha,
        recompute_cost=recompute_cost,
    )
    calibrated = contracts["__global__"]
    contract: dict[str, dict[str, float]] = {}
    audit: dict[str, Any] = {}
    for option in PLAN_MODES:
        entry = calibrated[option]
        if option == "direct-stale":
            delta = 0.0
            delta_source = "0 by construction: direct-stale never says fresh"
        else:
            delta = float(bootstrap_options[option]["delta_reported"])
            delta_source = bootstrap_options[option]["reported_source"]
        contract[option] = {
            "expected_cost": float(entry["expected_cost"]),
            "delta": delta,
        }
        audit[option] = {
            "delta_used": delta,
            "delta_source": delta_source,
            "delta_from_calibrate_contracts": float(entry["delta"]),
            "expected_cost": float(entry["expected_cost"]),
            "edge_misses": int(entry["edge_misses"]),
            "risk_edge_n": int(entry["risk_edge_n"]),
            "icc": float(entry["icc"]),
        }
    return contract, {
        "audit": audit,
        "expected_cost_source": source["expected_cost_source"],
        "recompute_cost": recompute_cost,
        "risk_edge_samples": source["risk_edge_samples_positive"],
        "risk_episode_samples": source["risk_episode_samples_positive"],
        "cost_samples": source["cost_samples_all_cases"],
    }


def cheapest_per_length(
    contract: dict[str, dict[str, float]], *, eps_prop: float
) -> dict[str, Any]:
    """For a uniform path of length L, the cheapest tier meeting L*delta <= eps."""
    out: dict[str, Any] = {}
    for length in range(1, MAX_PATH_LENGTH + 1):
        feasible = [
            (spec["expected_cost"], name)
            for name, spec in contract.items()
            if length * spec["delta"] <= eps_prop
        ]
        cheapest = min(feasible)
        out[f"L{length}"] = {
            "chosen": cheapest[1],
            "expected_cost": cheapest[0],
            "feasible_options": sorted(name for _, name in feasible),
            "path_risk": length * contract[cheapest[1]]["delta"],
            "headroom": eps_prop - length * contract[cheapest[1]]["delta"],
        }
    return out


def plan_per_episode(
    rows: list[dict[str, Any]],
    contract: dict[str, dict[str, float]],
    *,
    eps_prop: float,
) -> dict[str, Any]:
    """Run the real solver per episode. Secondary check -- see module docstring."""
    by_episode: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in rows:
        by_episode[str(row["episode_id"])].append(
            (str(row["dependency_id"]), str(row["dependent_id"]))
        )
    mix: dict[str, int] = {}
    topologies: dict[str, int] = {}
    solvers: dict[str, int] = {}
    infeasible: list[str] = []
    fell_back: list[str] = []
    blocked_reasons: dict[str, int] = {}
    for episode_id, edges in sorted(by_episode.items()):
        plan = plan_published_graph(
            edges,
            contract=contract,
            epsilon=eps_prop,
            contract_method="cp-upper",
            evaluation_episodes_in_calibration=True,
        )
        for mode, count in plan["planned_mode_mix"].items():
            mix[mode] = mix.get(mode, 0) + count
        topologies[plan["topology"]] = topologies.get(plan["topology"], 0) + 1
        solvers[plan["solver"]] = solvers.get(plan["solver"], 0) + 1
        if not plan["feasible"]:
            infeasible.append(episode_id)
        if plan["planner_fell_back"]:
            fell_back.append(episode_id)
        for reason in plan["certification_blocked_reason"]:
            blocked_reasons[reason] = blocked_reasons.get(reason, 0) + 1
    return {
        "n_episodes": len(by_episode),
        "planned_mode_mix": mix,
        "topologies": topologies,
        "solvers": solvers,
        "n_infeasible_episodes": len(infeasible),
        "infeasible_episodes": infeasible[:20],
        "n_planner_fell_back": len(fell_back),
        "certification_blocked_reason_counts": blocked_reasons,
        "edge_source": (
            "calibration edge set (candidate judge edges), NOT the frozen P1 v3 "
            "graph; realistic shapes, not an e2e mode-mix prediction"
        ),
    }


def analyze_hop(
    runs_dir: Path,
    bootstrap_hops: dict[str, Any],
    hop: int,
    *,
    eps_prop: float,
    recompute_costs: list[float],
    alpha: float,
) -> dict[str, Any]:
    edges_path = runs_dir / f"neg_edge_set_hop{hop}.json"
    verdicts_path = runs_dir / f"judge_routing_hop{hop}.json"
    rows = load_joined(edges_path, verdicts_path)
    bootstrap_options = bootstrap_hops[f"hop{hop}"]["options"]
    sweep: dict[str, Any] = {}
    for recompute_cost in recompute_costs:
        contract, provenance = build_contract(
            rows,
            bootstrap_options,
            recompute_cost=recompute_cost,
            alpha=alpha,
        )
        sweep[f"recompute_cost={recompute_cost:g}"] = {
            "contract": contract,
            "provenance": provenance,
            "per_length": cheapest_per_length(contract, eps_prop=eps_prop),
            "planner": plan_per_episode(rows, contract, eps_prop=eps_prop),
        }
    return {
        "inputs": {
            "edges": str(edges_path),
            "edges_sha256_16": sha256_16(edges_path),
            "verdicts": str(verdicts_path),
            "verdicts_sha256_16": sha256_16(verdicts_path),
        },
        "n_rows": len(rows),
        "n_episodes": len({str(row["episode_id"]) for row in rows}),
        "sweep": sweep,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", required=True)
    parser.add_argument("--bootstrap", required=True)
    parser.add_argument(
        "--eps-prop",
        type=float,
        required=True,
        help="propagation risk budget; a deployer input, so no default is offered",
    )
    parser.add_argument(
        "--recompute-costs",
        type=float,
        nargs="+",
        required=True,
        help=(
            "tokens charged per edge a tier marks stale; no calibrated source "
            "exists, so it is swept rather than chosen"
        ),
    )
    parser.add_argument("--alpha", type=float, required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    runs_dir = Path(args.runs_dir)
    bootstrap_path = Path(args.bootstrap)
    bootstrap = json.loads(bootstrap_path.read_text(encoding="utf-8"))

    report: dict[str, Any] = {
        "generated": "2026-09-08",
        "purpose": (
            "replace the placeholder propagation contract (all judging tiers "
            "delta=1.0, ordinal costs) with measured delta and measured judge "
            "cost, and sweep the one number that has no calibrated source: what "
            "one wrongly-withheld node costs in tokens"
        ),
        "delta_source": (
            f"{bootstrap_path} delta_reported = max(independent Clopper-Pearson, "
            "episode-cluster bootstrap); direct-stale is 0 by construction"
        ),
        "delta_source_sha256_16": sha256_16(bootstrap_path),
        "recompute_cost_provenance": (
            "no calibrated source; the proposal (S4.4) makes it a deployer input "
            "or requires the components be reported separately. Swept here. "
            "chronological_policy.RECOMPUTE_COST_SENSITIVITY = (100, 1000, 10000) "
            "with primary 1000 is the pre-existing convention; 0 is included to "
            "show the degenerate end where withholding is free."
        ),
        "eps_prop": args.eps_prop,
        "eps_prop_provenance": (
            "deployer input, set to 0.2 by user decision on 2026-09-03"
        ),
        "alpha": args.alpha,
        "contamination": (
            "delta is calibrated on the same episodes it is applied to (hop1 "
            "100/100, hop2 64/64, complete overlap). "
            "evaluation_episodes_in_calibration=True is passed to the planner so "
            "certified stays false. A held-out re-split was deliberately not done "
            "(2026-09-08): the analyst had already seen all episodes, so a split "
            "now would look like held-out validation without being it. "
            "Development-set numbers."
        ),
        "model_provenance": (
            "delta and judge tokens were measured with cheap=gpt-4o-mini and "
            "strong=claude-opus-4-8 (see judge_routing_hop*.json). These numbers "
            "commit any run that uses them to those two models."
        ),
        "hops": {},
    }
    for hop in HOPS:
        report["hops"][f"hop{hop}"] = analyze_hop(
            runs_dir,
            bootstrap["hops"],
            hop,
            eps_prop=args.eps_prop,
            recompute_costs=list(args.recompute_costs),
            alpha=args.alpha,
        )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    for hop in HOPS:
        hop_report = report["hops"][f"hop{hop}"]
        print(f"== hop{hop}  rows={hop_report['n_rows']} episodes={hop_report['n_episodes']}")
        for key, entry in hop_report["sweep"].items():
            per_length = entry["per_length"]
            chosen = " ".join(
                f"L{n}={per_length[f'L{n}']['chosen']}"
                for n in range(1, MAX_PATH_LENGTH + 1)
            )
            mix = entry["planner"]["planned_mode_mix"]
            print(f"   {key:24} {chosen}")
            print(f"{'':27}planner mix: {mix}")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
