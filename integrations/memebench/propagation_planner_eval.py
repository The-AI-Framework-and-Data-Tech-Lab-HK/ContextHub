"""Evaluate reliability-constrained propagation planning on MEME artifacts.

This module is deliberately DB/API free.  It joins the already materialized
edge and judge files, calibrates detector miss contracts, builds one graph per
episode, and delegates optimization to ``contexthub.planning``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, fields, is_dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from contexthub.planning.statistics import clopper_pearson_upper


@dataclass(frozen=True)
class OptionRecord:
    name: str
    cost: float
    delta: float


@dataclass(frozen=True)
class EdgeRecord:
    source: str
    target: str
    options: tuple[OptionRecord, ...]


@dataclass
class EpisodeProblem:
    episode_id: str
    edges: list[EdgeRecord]
    roots: list[str]
    targets: list[str]
    topology: str
    epsilon: float
    contract_source: dict[str, Any]


def design_effect(clusters: list[list[bool]]) -> tuple[float, float]:
    """Return (ICC, design effect) for binary outcomes grouped by cluster.

    Reported as a diagnostic only.  ``calibrate_contracts`` does NOT divide the
    sample size by this factor: the delta bound assumes edges are independent
    and uses the full edge count.

    The correction was applied until 2026-09-03 and was then removed by
    decision, because using it asserted an unverifiable claim about how
    path-adjacent edges correlate -- and asserting it while also using a union
    bound downstream is self-contradictory, since not needing an independence
    assumption is the only reason to pay for a union bound.

    Keeping the number in the artifact makes the dropped assumption auditable:
    where ICC is materially above 0, the independence assumption is known to be
    violated and the reported delta is anti-conservative (too small).

    A bound that does not assume independence now exists in
    ``calibration_cluster_bootstrap`` (episodes resampled whole, 2026-09-03).
    It is reported alongside this one there, not applied here: the miss rate it
    measures came out at most 0.019 higher, and the amount tracked ICC.
    """
    sizes = [len(c) for c in clusters if c]
    if len(sizes) < 2:
        return 0.0, 1.0
    n = sum(sizes)
    grand = sum(sum(c) for c in clusters) / n
    if grand in (0.0, 1.0):
        return 0.0, 1.0
    m_bar = n / len(sizes)
    between = sum(
        len(c) * (sum(c) / len(c) - grand) ** 2 for c in clusters if c
    ) / (len(sizes) - 1)
    within = grand * (1.0 - grand)
    if within <= 0.0:
        return 0.0, 1.0
    icc = (between - within) / (within * (m_bar - 1)) if m_bar > 1 else 0.0
    icc = min(max(icc, 0.0), 1.0)
    deff = max(1.0, 1.0 + (m_bar - 1.0) * icc)
    return icc, deff


def _prediction(verdict: dict[str, Any], option: str) -> bool:
    if option == "J1":
        return bool(verdict["j1_rule"])
    if option == "J3":
        return bool(verdict["j3_cheap"])
    if option == "J4":
        return bool(verdict["j4_costly"])
    if option == "cascade":
        return bool(verdict["j3_cheap"]) or bool(verdict["j4_costly"])
    if option == "direct-stale":
        return True
    raise KeyError(option)


def _stratum_name(row: dict[str, Any], stratum_key: str) -> str:
    return f"stratum:{row.get(stratum_key, '__missing__')}"


def _observed_cost(verdict: dict[str, Any], option: str, recompute_cost: float) -> float:
    if option == "J1":
        return recompute_cost if _prediction(verdict, option) else 0.0
    if option == "J3":
        return float(verdict["j3_tokens"]) + (
            recompute_cost if _prediction(verdict, option) else 0.0
        )
    if option == "J4":
        return float(verdict["j4_tokens"]) + (
            recompute_cost if _prediction(verdict, option) else 0.0
        )
    if option == "cascade":
        # J4 is paid only when J3 says fresh.  This is an observed execution
        # path, not a product of marginal call probabilities.
        stale3 = bool(verdict["j3_cheap"])
        tokens = float(verdict["j3_tokens"])
        if not stale3:
            tokens += float(verdict["j4_tokens"])
        return tokens + (recompute_cost if _prediction(verdict, option) else 0.0)
    if option == "direct-stale":
        return recompute_cost
    raise KeyError(option)


def load_joined(edges_path: str | Path, verdicts_path: str | Path) -> list[dict[str, Any]]:
    edge_doc = json.loads(Path(edges_path).read_text(encoding="utf-8"))
    verdict_doc = json.loads(Path(verdicts_path).read_text(encoding="utf-8"))
    edges, verdicts = edge_doc["edges"], verdict_doc["verdicts"]
    if len(edges) != len(verdicts):
        raise ValueError(f"edge/verdict length mismatch: {len(edges)} != {len(verdicts)}")
    joined = []
    for index, (edge, verdict) in enumerate(zip(edges, verdicts)):
        if bool(edge["should_stale"]) != bool(verdict["should_stale"]):
            raise ValueError(f"edge/verdict label mismatch at index {index}")
        if verdict.get("error"):
            continue
        joined.append({**edge, "_verdict": verdict})
    return joined


def split_episodes(
    rows: Iterable[dict[str, Any]],
    *,
    seed: str,
    calibration_fraction: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Stable episode-level split, independent of input ordering."""
    if not seed:
        raise ValueError("split seed must be non-empty")
    if not 0 < calibration_fraction < 1:
        raise ValueError("calibration_fraction must be in (0, 1)")
    materialized = list(rows)
    episode_ids = sorted({str(row["episode_id"]) for row in materialized})
    assignments = []
    calibration_ids: set[str] = set()
    for episode_id in episode_ids:
        digest = hashlib.sha256(f"{seed}\0{episode_id}".encode("utf-8")).hexdigest()
        score = int(digest, 16) / (1 << 256)
        bucket = "calibration" if score < calibration_fraction else "evaluation"
        if bucket == "calibration":
            calibration_ids.add(episode_id)
        assignments.append({
            "episode_id": episode_id,
            "sha256": digest,
            "score": score,
            "split": bucket,
        })
    evaluation_ids = set(episode_ids) - calibration_ids
    if not calibration_ids or not evaluation_ids:
        raise ValueError(
            "stable episode split produced an empty side; change seed/fraction "
            f"(calibration={len(calibration_ids)}, evaluation={len(evaluation_ids)})"
        )
    calibration = [
        row for row in materialized if str(row["episode_id"]) in calibration_ids
    ]
    evaluation = [
        row for row in materialized if str(row["episode_id"]) in evaluation_ids
    ]
    manifest = {
        "algorithm": "sha256(seed + NUL + episode_id), score=hash/2^256",
        "seed": seed,
        "calibration_fraction": calibration_fraction,
        "calibration_episode_ids": sorted(calibration_ids),
        "evaluation_episode_ids": sorted(evaluation_ids),
        "assignments": assignments,
    }
    return calibration, evaluation, manifest


def calibrate_contracts(
    rows: Iterable[dict[str, Any]],
    *,
    method: str = "cp-upper",
    alpha: float = 0.05,
    stratum_key: str | None = None,
    recompute_cost: float = 0.0,
) -> tuple[dict[str, dict[str, dict[str, float | int]]], dict[str, Any]]:
    """Estimate risk on positives and expected cost on all calibration cases."""
    if method not in {"point", "cp-upper"}:
        raise ValueError("method must be point or cp-upper")
    materialized = list(rows)
    positives = [r for r in materialized if r["should_stale"]]
    risk_groups: dict[str, list[dict[str, Any]]] = {"__global__": positives}
    cost_groups: dict[str, list[dict[str, Any]]] = {"__global__": materialized}
    for row in positives:
        if stratum_key:
            key = _stratum_name(row, stratum_key)
            risk_groups.setdefault(key, []).append(row)
    if stratum_key:
        for row in materialized:
            key = _stratum_name(row, stratum_key)
            cost_groups.setdefault(key, []).append(row)

    # The planner selects among all four stochastic bundles after seeing their
    # bounds. Bonferroni keeps those bounds simultaneously valid over every
    # published stratum, including the global fallback.
    stochastic_options = ("J1", "J3", "J4", "cascade")
    active_groups = [key for key, group in risk_groups.items() if group]
    corrected_alpha = (
        alpha / (len(active_groups) * len(stochastic_options))
        if method == "cp-upper"
        else None
    )
    contracts: dict[str, dict[str, dict[str, float | int]]] = {}
    risk_samples: dict[str, int] = {}
    risk_edge_samples: dict[str, int] = {}
    risk_episode_samples: dict[str, int] = {}
    cost_samples: dict[str, int] = {}
    for key, risk_group in sorted(risk_groups.items()):
        if not risk_group:
            continue
        cost_group = cost_groups.get(key) or materialized
        episode_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in risk_group:
            episode_groups[str(row["episode_id"])].append(row)
        risk_samples[key] = len(risk_group)
        risk_episode_samples[key] = len(episode_groups)
        risk_edge_samples[key] = len(risk_group)
        cost_samples[key] = len(cost_group)
        contracts[key] = {}
        for option in ("J1", "J3", "J4", "cascade", "direct-stale"):
            edge_misses = sum(
                not _prediction(row["_verdict"], option) for row in risk_group
            )
            # delta is a per-edge miss probability, so it must be counted per
            # edge.  Episodes only shrink the effective denominator; they are
            # not the risk unit.  Counting per-episode any-miss inflated the
            # numerator and shrank the denominator at the same time.
            miss_clusters = [
                [not _prediction(row["_verdict"], option) for row in episode_rows]
                for episode_rows in episode_groups.values()
            ]
            # The bound treats edges as independent: the full edge count is the
            # denominator and no clustering correction is applied.  ICC and the
            # design effect are still computed and reported so the strength of
            # that assumption stays auditable in the artifact, but they do not
            # enter delta.  See `design_effect` for why they are not applied.
            icc, deff = design_effect(miss_clusters)
            delta = (
                edge_misses / len(risk_group)
                if method == "point"
                else clopper_pearson_upper(
                    edge_misses,
                    len(risk_group),
                    corrected_alpha,
                )
            )
            expected_cost = sum(
                _observed_cost(row["_verdict"], option, recompute_cost)
                for row in cost_group
            ) / len(cost_group)
            contracts[key][option] = {
                "delta": 0.0 if option == "direct-stale" else delta,
                "expected_cost": expected_cost,
                "risk_n": risk_samples[key],
                "risk_edge_n": len(risk_group),
                "risk_episode_n": len(episode_groups),
                "edge_misses": edge_misses,
                # Diagnostics only -- reported, not applied to delta.
                "icc": icc,
                "design_effect": deff,
                "design_effect_applied": False,
                "cost_n": len(cost_group),
            }
    if not contracts:
        raise ValueError("no positive calibration edges")
    source = {
        "method": method,
        "alpha": alpha if method == "cp-upper" else None,
        "per_contract_alpha": corrected_alpha,
        "simultaneous_contracts": (
            len(active_groups) * len(stochastic_options)
            if method == "cp-upper"
            else None
        ),
        "multiplicity_correction": (
            "Bonferroni over stochastic options and published strata"
            if method == "cp-upper"
            else None
        ),
        "scope": "stratum" if stratum_key else "global",
        "stratum_key": stratum_key,
        "risk_samples_positive": risk_samples,
        "risk_edge_samples_positive": risk_edge_samples,
        "risk_episode_samples_positive": risk_episode_samples,
        "cost_samples_all_cases": cost_samples,
        "risk_conditioning": (
            "delta is a per-edge miss probability and is counted per edge. "
            "ASSUMPTION: edges are treated as independent -- the Clopper-Pearson "
            "bound uses the full edge count as its denominator and applies no "
            "clustering correction. This assumption is known to be violated in "
            "this data (see the reported per-contract 'icc'; a separate 2x2 "
            "analysis measured deff around 1.83 on the strong tier), so where ICC "
            "is materially above 0 the reported delta is ANTI-CONSERVATIVE, i.e. "
            "too small. The ICC and design_effect fields are diagnostics only and "
            "are not applied ('design_effect_applied': false); the design-effect "
            "correction was applied until 2026-09-03 and removed by decision, "
            "because asserting a correlation model here while relying on a union "
            "bound downstream is self-contradictory. A bound that does not assume "
            "independence exists in calibration_cluster_bootstrap (whole episodes "
            "resampled, 2026-09-03; at most 0.019 higher, tracking ICC) and is "
            "reported against this one there, not applied here. "
            "Users must additionally declare their own clustering "
            "unit: 'episode' is a choice inherited from how MEME ships data, not "
            "a natural unit. Point mode is descriptive edge-level risk."
        ),
        "cost_conditioning": "all calibration change-edge cases",
        "expected_cost_source": (
            "calibration mean: observed judge bundle tokens + "
            "observed stale probability * recompute_cost"
        ),
        "recompute_cost": recompute_cost,
        "unseen_stratum_policy": "fall back to calibration __global__ contract",
        "statistical_status": (
            "descriptive point estimate, not a confidence certificate"
            if method == "point"
            else (
                "one-sided episode-cluster Clopper-Pearson upper bound with "
                "Bonferroni multiplicity correction"
            )
        ),
    }
    return contracts, source


def _classify(edges: list[tuple[str, str]]) -> tuple[list[str], list[str], str]:
    nodes = {x for edge in edges for x in edge}
    indegree = {n: 0 for n in nodes}
    outdegree = {n: 0 for n in nodes}
    adjacency: dict[str, list[str]] = defaultdict(list)
    for source, target in edges:
        indegree[target] += 1
        outdegree[source] += 1
        adjacency[source].append(target)
    roots = sorted(n for n in nodes if indegree[n] == 0)
    targets = sorted(n for n in nodes if outdegree[n] == 0)
    pending = roots[:]
    seen = 0
    degree = indegree.copy()
    while pending:
        node = pending.pop()
        seen += 1
        for child in adjacency[node]:
            degree[child] -= 1
            if degree[child] == 0:
                pending.append(child)
    if seen != len(nodes):
        raise ValueError("episode graph contains a cycle")
    if all(indegree[n] <= 1 and outdegree[n] <= 1 for n in nodes):
        topology = "chain"
    elif all(indegree[n] <= 1 for n in nodes):
        topology = "tree"
    else:
        topology = "dag"
    return roots, targets, topology


def build_episode_problems(
    rows: Iterable[dict[str, Any]],
    contracts: dict[str, dict[str, dict[str, float | int]]],
    contract_source: dict[str, Any],
    *,
    epsilon: float,
) -> list[EpisodeProblem]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        # The runtime planner sees the published dependency graph before it
        # knows which edges this particular change truly affects. Including
        # only should_stale=True edges would use evaluation gold to prune the
        # graph and understate both planning cost and all-path constraints.
        grouped[str(row["episode_id"])].append(row)
    result = []
    stratum_key = contract_source.get("stratum_key")
    for episode, episode_rows in sorted(grouped.items()):
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for row in episode_rows:
            unique.setdefault((str(row["dependency_id"]), str(row["dependent_id"])), row)
        pairs = list(unique)
        roots, targets, topology = _classify(pairs)
        edge_records = []
        for (source, target), row in unique.items():
            key = _stratum_name(row, stratum_key) if stratum_key else "__global__"
            menu_contract = contracts.get(key, contracts["__global__"])
            options = tuple(
                OptionRecord(
                    name=name,
                    cost=float(menu_contract[name]["expected_cost"]),
                    delta=float(menu_contract[name]["delta"]),
                )
                for name in ("J1", "J3", "J4", "cascade", "direct-stale")
            )
            edge_records.append(EdgeRecord(source, target, options))
        result.append(EpisodeProblem(
            episode_id=episode,
            edges=edge_records,
            roots=roots,
            targets=targets,
            topology=topology,
            epsilon=epsilon,
            contract_source=contract_source,
        ))
    return result


def _load_core():
    try:
        models = importlib.import_module("contexthub.planning.models")
        planners = importlib.import_module("contexthub.planning.planners")
        return SimpleNamespace(
            EdgeOption=models.EdgeOption,
            PolicyCandidate=models.PolicyCandidate,
            GraphProblem=models.GraphProblem,
            PlanResult=models.PlanResult,
            brute_force=planners.brute_force_plan,
            uniform=planners.uniform_epsilon_plan,
            independent=planners.independent_edge_plan,
            chain=planners.chain_risk_grid_plan,
            tree=planners.tree_risk_grid_plan,
            milp=planners.dag_milp_plan,
            heuristic=planners.repair_large_dag_plan,
        )
    except ImportError as split_error:
        try:
            return importlib.import_module("contexthub.planning")
        except ImportError as package_error:
            raise ImportError(
                f"planning core unavailable: {split_error}; {package_error}"
            ) from package_error


def _construct(cls, values: dict[str, Any]):
    params = inspect.signature(cls).parameters
    aliases = {
        "risk": "delta", "miss_probability": "delta", "id": "name",
        "src": "source", "dst": "target", "budget": "epsilon",
        "edge_options": "edges",
    }
    kwargs = {}
    for parameter in params:
        key = aliases.get(parameter, parameter)
        if key in values:
            kwargs[parameter] = values[key]
    return cls(**kwargs)


def to_core_problem(problem: EpisodeProblem, core=None):
    core = core or _load_core()
    if hasattr(core, "PolicyCandidate"):
        core_edges = [
            core.EdgeOption(
                upstream=edge.source,
                dependent=edge.target,
                candidates=tuple(
                    core.PolicyCandidate(option.name, option.cost, option.delta)
                    for option in edge.options
                ),
            )
            for edge in problem.edges
        ]
        nodes = sorted({endpoint for edge in problem.edges for endpoint in (edge.source, edge.target)})
        return core.GraphProblem(
            nodes=tuple(nodes),
            edges=tuple(core_edges),
            sources=tuple(problem.roots),
            targets=tuple(problem.targets),
            epsilon=problem.epsilon,
        )
    core_edges = []
    for edge in problem.edges:
        options = [
            _construct(core.EdgeOption, asdict(option))
            for option in edge.options
        ]
        values = {
            "source": edge.source, "target": edge.target, "options": options,
            "name": f"{edge.source}->{edge.target}",
        }
        edge_cls = getattr(core, "GraphEdge", None)
        core_edges.append(_construct(edge_cls, values) if edge_cls else (edge.source, edge.target, options))
    return _construct(core.GraphProblem, {
        "edges": core_edges, "roots": problem.roots, "targets": problem.targets,
        "epsilon": problem.epsilon,
    })


def _call_solver(core, name: str, core_problem):
    candidates = {
        "brute_force": ("brute_force",),
        "uniform": ("uniform",),
        "independent": ("independent",),
        "chain": ("solve_chain", "chain"),
        "tree": ("solve_tree", "tree"),
        "milp": ("solve_milp", "milp"),
        "heuristic": ("solve_heuristic", "heuristic"),
    }[name]
    for candidate in candidates:
        function = getattr(core, candidate, None)
        if callable(function):
            return function(core_problem)
    raise AttributeError(f"planning core has no {name} solver ({', '.join(candidates)})")


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if hasattr(value, "items"):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _result_field(result: Any, *names: str, default=None):
    for name in names:
        if isinstance(result, dict) and name in result:
            return result[name]
        if hasattr(result, name):
            return getattr(result, name)
    return default


def solve_episode(problem: EpisodeProblem, *, core=None, brute_force_max_edges: int = 10) -> dict[str, Any]:
    core = core or _load_core()
    core_problem = to_core_problem(problem, core)
    solver = "chain" if problem.topology == "chain" else (
        "tree" if problem.topology == "tree" else "milp"
    )
    try:
        solved = _call_solver(core, solver, core_problem)
    except (AttributeError, NotImplementedError, RuntimeError):
        solver = "heuristic"
        solved = _call_solver(core, solver, core_problem)
    baseline_results = {}
    for baseline in ("uniform", "independent"):
        try:
            baseline_results[baseline] = _jsonable(_call_solver(core, baseline, core_problem))
        except (AttributeError, NotImplementedError):
            pass
    oracle = None
    combinations = math.prod(len(edge.options) for edge in problem.edges)
    if len(problem.edges) <= brute_force_max_edges and combinations <= 1_000_000:
        try:
            oracle = _call_solver(core, "brute_force", core_problem)
        except (AttributeError, NotImplementedError, ValueError):
            pass
    cost = _result_field(solved, "cost", "total_cost")
    oracle_cost = _result_field(oracle, "cost", "total_cost") if oracle is not None else None
    lower_bound = sum(min(option.cost for option in edge.options) for edge in problem.edges)
    exact_gap = (
        (float(cost) - float(oracle_cost)) / max(abs(float(oracle_cost)), 1e-12)
        if cost is not None and oracle_cost is not None else
        None
    )
    lower_bound_gap = (
        (float(cost) - lower_bound) / lower_bound
        if cost is not None and lower_bound > 0
        else (0.0 if cost == 0 and lower_bound == 0 else None)
    )
    if problem.topology not in {"chain", "tree"}:
        certification_scope = "non-tree algorithm diagnostic under assumed contract"
    elif problem.contract_source.get("method") == "point":
        certification_scope = "held-out tree evaluation; descriptive point contract only"
    else:
        certification_scope = (
            "held-out tree evaluation with calibration-split confidence bound; "
            "not deployment certification"
        )
    return {
        "episode_id": problem.episode_id,
        "topology": problem.topology,
        "certification_scope": certification_scope,
        "solver": solver,
        "baseline": baseline_results,
        "cost": cost,
        "feasible": _result_field(solved, "feasible", default=None),
        "max_path_risk": _result_field(solved, "max_path_risk", "risk", default=None),
        "gap": exact_gap if exact_gap is not None else lower_bound_gap,
        "gap_kind": "exact-opt" if exact_gap is not None else "cheapest-menu-lower-bound",
        "lower_bound": oracle_cost if oracle_cost is not None else lower_bound,
        "n_edges": len(problem.edges),
        "n_options": sum(len(edge.options) for edge in problem.edges),
        "roots": problem.roots,
        "targets": problem.targets,
        "contract_source": problem.contract_source,
        "result": _jsonable(solved),
        "brute_force": _jsonable(oracle) if oracle is not None else None,
    }


def evaluate(
    edges_path: str | Path,
    verdicts_path: str | Path,
    *,
    epsilon: float,
    contract_method: str = "cp-upper",
    alpha: float = 0.05,
    stratum_key: str | None = None,
    recompute_cost: float = 0.0,
    brute_force_max_edges: int = 10,
    split_seed: str,
    calibration_fraction: float,
    core=None,
) -> dict[str, Any]:
    rows = load_joined(edges_path, verdicts_path)
    calibration_rows, evaluation_rows, split = split_episodes(
        rows, seed=split_seed, calibration_fraction=calibration_fraction
    )
    contracts, source = calibrate_contracts(
        calibration_rows, method=contract_method, alpha=alpha,
        stratum_key=stratum_key, recompute_cost=recompute_cost,
    )
    problems = build_episode_problems(
        evaluation_rows, contracts, source, epsilon=epsilon
    )
    results = [
        solve_episode(p, core=core, brute_force_max_edges=brute_force_max_edges)
        for p in problems
    ]
    return {
        "inputs": {"edges": str(edges_path), "verdicts": str(verdicts_path)},
        "epsilon": epsilon,
        "contract_source": source,
        "contracts": contracts,
        "split": split,
        "certification_scope": (
            "held-out workload evaluation; deployment certification is not "
            "complete. Only tree/forest instances can support the current "
            "empirical contract claim; non-tree results are algorithm diagnostics "
            "under assumed contracts"
        ),
        "n_joined": len(rows),
        "n_calibration_rows": len(calibration_rows),
        "n_evaluation_rows": len(evaluation_rows),
        "n_calibration_positive": sum(bool(r["should_stale"]) for r in calibration_rows),
        "n_evaluation_positive": sum(bool(r["should_stale"]) for r in evaluation_rows),
        "n_episodes": len(problems),
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edges", required=True)
    parser.add_argument("--verdicts", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--epsilon", required=True, type=float)
    parser.add_argument("--contract", choices=["point", "cp-upper"], default="cp-upper")
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--stratum-key", default=None)
    parser.add_argument("--recompute-cost", type=float, default=0.0)
    parser.add_argument("--brute-force-max-edges", type=int, default=10)
    parser.add_argument("--split-seed", required=True)
    parser.add_argument("--calibration-fraction", required=True, type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = evaluate(
        args.edges, args.verdicts, epsilon=args.epsilon,
        contract_method=args.contract, alpha=args.alpha,
        stratum_key=args.stratum_key, recompute_cost=args.recompute_cost,
        brute_force_max_edges=args.brute_force_max_edges,
        split_seed=args.split_seed,
        calibration_fraction=args.calibration_fraction,
    )
    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
