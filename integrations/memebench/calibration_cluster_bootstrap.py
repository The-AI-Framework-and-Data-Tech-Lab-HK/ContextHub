"""Episode-cluster bootstrap for the propagation miss-rate contract (delta).

Why this exists
---------------
``propagation_planner_eval.calibrate_contracts`` reports delta as a
Clopper-Pearson upper bound whose denominator is the full edge count, i.e. it
treats edges inside an episode as independent.  That assumption is known to be
violated on this data (the per-contract ``icc`` field), which makes the
reported delta ANTI-CONSERVATIVE where ICC is materially above zero.

The design-effect discount that used to sit in front of this problem was
removed on 2026-09-03, because asserting a correlation model there while
relying on a union bound downstream is self-contradictory.  Resampling whole
episodes with replacement is the remaining way to get an upper bound that does
not assume independence: clustering enters through the resampling scheme rather
than through an assumed ICC.

Both bounds are written side by side and the reported delta is the more
conservative (larger) of the two, per instruction.

Zero API: reads only neg_edge_set_hop{1,2}.json + judge_routing_hop{1,2}.json.

Usage:
    .venv/bin/python -m integrations.memebench.calibration_cluster_bootstrap \
        --runs-dir integrations/memebench/runs \
        --iters 20000 --seed 20260903 --alpha 0.05 \
        --out <dir>/calibration_bootstrap.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from contexthub.planning.statistics import clopper_pearson_upper

from integrations.memebench.propagation_planner_eval import (
    _prediction,
    design_effect,
    load_joined,
)

OPTIONS = ("J1", "J3", "J4", "cascade")
HOPS = (1, 2)


def sha256_16(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def miss_clusters(rows: list[dict[str, Any]], option: str) -> list[list[bool]]:
    """Group per-edge miss indicators by episode.

    A miss is an edge that should have gone stale and was judged fresh, which is
    exactly the event delta bounds.
    """
    groups: dict[str, list[bool]] = defaultdict(list)
    for row in rows:
        groups[str(row["episode_id"])].append(not _prediction(row["_verdict"], option))
    return list(groups.values())


def cluster_bootstrap(
    clusters: list[list[bool]],
    *,
    iters: int,
    alpha: float,
    rng: random.Random,
) -> dict[str, Any]:
    """Percentile upper bound on the pooled miss rate, resampling episodes.

    Each replicate draws ``len(clusters)`` episodes with replacement and keeps
    every edge of a drawn episode, so within-episode dependence is carried into
    the replicate whatever its form.  The estimator is the pooled ratio
    (total misses / total edges), which is what delta means; the number of edges
    therefore varies across replicates, as it should.
    """
    n_clusters = len(clusters)
    if n_clusters < 2:
        raise ValueError("cluster bootstrap needs at least two episodes")
    draws: list[float] = []
    for _ in range(iters):
        misses = 0
        edges = 0
        for _ in range(n_clusters):
            cluster = clusters[rng.randrange(n_clusters)]
            misses += sum(cluster)
            edges += len(cluster)
        draws.append(misses / edges)
    draws.sort()
    # Upper endpoint of a one-sided (1 - alpha) percentile interval.
    index = min(len(draws) - 1, max(0, int(round((1.0 - alpha) * (len(draws) - 1)))))
    return {
        "percentile_upper": draws[index],
        "point": sum(sum(c) for c in clusters) / sum(len(c) for c in clusters),
        "mean": statistics.fmean(draws),
        "std": statistics.pstdev(draws),
        "quantiles": {
            "p2.5": draws[int(round(0.025 * (len(draws) - 1)))],
            "p50": draws[int(round(0.5 * (len(draws) - 1)))],
            "p97.5": draws[int(round(0.975 * (len(draws) - 1)))],
        },
        "iters": iters,
        "alpha_one_sided": alpha,
    }


def replicated_bootstrap(
    clusters: list[list[bool]],
    *,
    iters: int,
    replicates: int,
    alpha: float,
    seed_prefix: str,
) -> dict[str, Any]:
    """Run independent bootstrap streams and take the upper envelope.

    A single stream's percentile endpoint carries Monte-Carlo noise of order
    0.001 at this alpha, which is larger than the gap between the two bounds for
    the low-ICC options.  Letting one stream decide which bound is larger would
    make the reported delta depend on the seed, so the reported bootstrap value
    is the max across replicates and the spread is kept as a noise band.
    """
    if replicates < 2:
        raise ValueError("need at least two replicates to measure MC noise")
    runs = [
        cluster_bootstrap(
            clusters,
            iters=iters,
            alpha=alpha,
            rng=random.Random(f"{seed_prefix}\0replicate{index}"),
        )
        for index in range(replicates)
    ]
    uppers = [run["percentile_upper"] for run in runs]
    return {
        "percentile_upper": max(uppers),
        "point": runs[0]["point"],
        "std": statistics.fmean(run["std"] for run in runs),
        "quantiles": runs[0]["quantiles"],
        "iters_per_replicate": iters,
        "replicates": replicates,
        "alpha_one_sided": alpha,
        "mc_noise": {
            "min_upper": min(uppers),
            "max_upper": max(uppers),
            "spread": max(uppers) - min(uppers),
            "mean_upper": statistics.fmean(uppers),
            "per_replicate_upper": uppers,
        },
    }


def bootstrap_design_effect(
    boot: dict[str, Any],
    *,
    n_edges: int,
) -> dict[str, Any]:
    """Variance-ratio design effect implied by the bootstrap spread.

    This is a diagnostic that makes the two bounds comparable: it says how much
    wider the cluster-resampled sampling distribution is than the binomial one
    at the same point estimate.  It is NOT applied to any reported delta -- that
    would reintroduce the discount removed on 2026-09-03, just with a bootstrap
    variance instead of an ICC.
    """
    p = boot["point"]
    binomial_var = p * (1.0 - p) / n_edges
    boot_var = boot["std"] ** 2
    return {
        "binomial_std": binomial_var**0.5,
        "bootstrap_std": boot["std"],
        "variance_ratio": (boot_var / binomial_var) if binomial_var > 0 else None,
        "applied": False,
    }


def compare_bounds(independent: float, boot: dict[str, Any]) -> dict[str, Any]:
    """Decide, and say whether the decision is resolvable at this MC noise.

    ``max`` is applied either way -- it is the conservative choice by
    instruction.  What the noise band changes is what may be *claimed*: when the
    gap is inside the band, the two bounds are indistinguishable here and the
    third decimal must not be reported as a difference between methods.
    """
    upper = boot["percentile_upper"]
    gap = upper - independent
    band = boot["mc_noise"]["spread"]
    return {
        "delta_reported": max(independent, upper),
        "reported_source": "cluster-bootstrap" if upper > independent else "independent-cp",
        "gap": gap,
        "mc_noise_band": band,
        "gap_exceeds_mc_noise": abs(gap) > band,
        "separation": (
            "bootstrap materially larger"
            if gap > band
            else "indistinguishable at this MC noise"
            if abs(gap) <= band
            else "independent-cp larger"
        ),
    }


def analyze_hop(
    runs_dir: Path,
    hop: int,
    *,
    iters: int,
    replicates: int,
    alpha: float,
    seed: int,
    eps_prop: float,
) -> dict[str, Any]:
    edges_path = runs_dir / f"neg_edge_set_hop{hop}.json"
    verdicts_path = runs_dir / f"judge_routing_hop{hop}.json"
    rows = load_joined(edges_path, verdicts_path)
    positives = [row for row in rows if row["should_stale"]]
    if not positives:
        raise ValueError(f"hop{hop}: no positive edges")

    # Same Bonferroni family as calibrate_contracts: the planner picks among the
    # four stochastic options after seeing their bounds, global scope only, so
    # both bounds are corrected identically and stay comparable.
    corrected_alpha = alpha / len(OPTIONS)

    n_edges = len(positives)
    episode_ids = sorted({str(row["episode_id"]) for row in positives})
    out: dict[str, Any] = {
        "inputs": {
            "edges": str(edges_path),
            "edges_sha256_16": sha256_16(edges_path),
            "verdicts": str(verdicts_path),
            "verdicts_sha256_16": sha256_16(verdicts_path),
        },
        "risk_edge_n": n_edges,
        "risk_episode_n": len(episode_ids),
        "alpha": alpha,
        "per_contract_alpha": corrected_alpha,
        "options": {},
    }

    for position, option in enumerate(OPTIONS):
        clusters = miss_clusters(positives, option)
        misses = sum(sum(c) for c in clusters)
        # One stream family per (hop, option) so adding or reordering options
        # cannot shift another option's draws.
        boot = replicated_bootstrap(
            clusters,
            iters=iters,
            replicates=replicates,
            alpha=corrected_alpha,
            seed_prefix=f"{seed}\0{hop}\0{option}\0{position}",
        )
        independent = clopper_pearson_upper(misses, n_edges, corrected_alpha)
        icc, deff = design_effect(clusters)
        verdict = compare_bounds(independent, boot)
        out["options"][option] = {
            "edge_misses": misses,
            "risk_edge_n": n_edges,
            "point_estimate": misses / n_edges,
            "delta_independent_cp": independent,
            "delta_cluster_bootstrap": boot["percentile_upper"],
            **verdict,
            "bootstrap": boot,
            "bootstrap_design_effect": bootstrap_design_effect(boot, n_edges=n_edges),
            "icc": icc,
            "icc_design_effect": deff,
            "design_effect_applied": False,
            "misses_to_flip": {
                f"{length}hop": misses_to_flip(
                    clusters,
                    n_edges=n_edges,
                    alpha=corrected_alpha,
                    budget=eps_prop,
                    path_length=length,
                )
                for length in (1, 2)
            },
        }
    return out


def feasibility(hop_report: dict[str, Any], *, eps_prop: float) -> dict[str, Any]:
    """What the reported deltas allow at a given propagation risk budget.

    The union bound over a path is a plain sum of the per-edge deltas, so a
    uniform-option path of length L is feasible iff ``L * delta <= eps_prop``.
    The budget is a deployer input, not a result, so it is a required argument:
    hard-coding one here is how ``delta=1.0`` once silently locked a whole run.

    ``headroom`` is what remains of the budget, and ``misses_to_flip`` says how
    many additional missed edges in the calibration set would push the option
    out of feasibility -- a thin margin means the verdict is an artifact of this
    sample, not a property of the method.
    """
    out: dict[str, Any] = {"eps_prop": eps_prop, "per_option": {}}
    for option, stats in hop_report["options"].items():
        delta = stats["delta_reported"]
        max_len = int(eps_prop // delta) if delta > 0 else None
        entry: dict[str, Any] = {
            "delta_reported": delta,
            "max_uniform_path_length": max_len,
            "feasible_1hop": delta <= eps_prop,
            "feasible_2hop": 2 * delta <= eps_prop,
            "union_bound_2hop": 2 * delta,
        }
        for length in (1, 2):
            used = length * delta
            entry[f"headroom_{length}hop"] = eps_prop - used
        out["per_option"][option] = entry
    return out


def misses_to_flip(
    clusters: list[list[bool]],
    *,
    n_edges: int,
    alpha: float,
    budget: float,
    path_length: int,
) -> int | None:
    """Extra calibration misses that would make this option infeasible.

    Recomputed through the same independent Clopper-Pearson bound, which is a
    lower bound on how fragile the verdict is: the bootstrap side generally sits
    higher, so it would flip no later than this.  ``None`` means already
    infeasible.
    """
    observed = sum(sum(c) for c in clusters)
    if path_length * clopper_pearson_upper(observed, n_edges, alpha) > budget:
        return None
    for extra in range(1, n_edges - observed + 1):
        bound = clopper_pearson_upper(observed + extra, n_edges, alpha)
        if path_length * bound > budget:
            return extra
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", required=True)
    parser.add_argument("--iters", type=int, required=True)
    parser.add_argument("--replicates", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--alpha", type=float, required=True)
    parser.add_argument(
        "--eps-prop",
        type=float,
        required=True,
        help="propagation risk budget; a deployer input, so no default is offered",
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    runs_dir = Path(args.runs_dir)
    report: dict[str, Any] = {
        "generated": "2026-09-03",
        "purpose": (
            "delta under episode-cluster bootstrap (no independence assumption) "
            "reported side by side with the independent Clopper-Pearson bound; "
            "delta_reported is the larger of the two"
        ),
        "estimator": (
            "pooled ratio (total misses / total edges) over episodes resampled "
            "with replacement; one-sided percentile upper endpoint, upper "
            "envelope over independent replicates"
        ),
        "multiplicity": f"Bonferroni alpha/{len(OPTIONS)} over stochastic options, global scope",
        "caveat": (
            "the percentile bootstrap cannot exceed the observed range, so it "
            "certifies 0 when 0 misses are seen; it is therefore reported "
            "against, not instead of, the Clopper-Pearson bound. Where "
            "'gap_exceeds_mc_noise' is false the two bounds are "
            "indistinguishable at this iteration count and their difference "
            "must not be read as a method effect."
        ),
        "iters_per_replicate": args.iters,
        "replicates": args.replicates,
        "seed": args.seed,
        "alpha": args.alpha,
        "eps_prop": args.eps_prop,
        "eps_prop_provenance": (
            "deployer input, set to 0.2 by user decision on 2026-09-03; the "
            "proposal treats it as an input rather than a result (S4.1 inputs, "
            "S4.4 'deployers set eps_prop'), and the 0.10 in the proposal's "
            "worked example is illustrative"
        ),
        "hops": {},
    }

    for hop in HOPS:
        hop_report = analyze_hop(
            runs_dir,
            hop,
            iters=args.iters,
            replicates=args.replicates,
            alpha=args.alpha,
            seed=args.seed,
            eps_prop=args.eps_prop,
        )
        hop_report["feasibility"] = feasibility(hop_report, eps_prop=args.eps_prop)
        report["hops"][f"hop{hop}"] = hop_report

        print(
            f"=== hop{hop}: {hop_report['risk_edge_n']} positive edges in "
            f"{hop_report['risk_episode_n']} episodes "
            f"(per-contract alpha {hop_report['per_contract_alpha']}) ==="
        )
        print(
            f"  {'option':9s} {'misses':>7s} {'point':>8s} {'indep-CP':>9s} "
            f"{'boot':>8s} {'gap':>8s} {'+-MC':>7s} {'reported':>9s} {'src':>6s} "
            f"{'ICC':>6s} {'vratio':>7s}  separation"
        )
        for option, stats in hop_report["options"].items():
            print(
                f"  {option:9s} {stats['edge_misses']:7d} "
                f"{stats['point_estimate']:8.4f} {stats['delta_independent_cp']:9.4f} "
                f"{stats['delta_cluster_bootstrap']:8.4f} {stats['gap']:+8.4f} "
                f"{stats['mc_noise_band']:7.4f} {stats['delta_reported']:9.4f} "
                f"{'boot' if stats['reported_source'] == 'cluster-bootstrap' else 'cp':>6s} "
                f"{stats['icc']:6.3f} "
                f"{stats['bootstrap_design_effect']['variance_ratio']:7.2f}"
                f"  {stats['separation']}"
            )
        feasible = hop_report["feasibility"]
        print(f"  -- feasibility at eps_prop={feasible['eps_prop']} --")
        print(
            f"  {'option':9s} {'delta':>8s} {'maxlen':>6s} {'1hop':>5s} {'2hop':>5s} "
            f"{'2hop sum':>8s} {'2hop room':>9s} {'flip@1':>6s} {'flip@2':>6s}"
        )
        for option, entry in feasible["per_option"].items():
            flip = hop_report["options"][option]["misses_to_flip"]
            print(
                f"  {option:9s} {entry['delta_reported']:8.4f} "
                f"{str(entry['max_uniform_path_length']):>6s} "
                f"{'yes' if entry['feasible_1hop'] else 'NO':>5s} "
                f"{'yes' if entry['feasible_2hop'] else 'NO':>5s} "
                f"{entry['union_bound_2hop']:8.4f} {entry['headroom_2hop']:+9.4f} "
                f"{str(flip['1hop']):>6s} {str(flip['2hop']):>6s}"
            )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, sort_keys=True)
    out_path.write_text(payload, encoding="utf-8")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    print(f"\nWrote {out_path}\ncontent sha256_16 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
