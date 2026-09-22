"""Reproducible synthetic evaluation for propagation-planner topology regimes."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from integrations.memebench.propagation_planner_eval import (
    EdgeRecord,
    EpisodeProblem,
    OptionRecord,
    _call_solver,
    _jsonable,
    _load_core,
    _result_field,
    to_core_problem,
)


@dataclass(frozen=True)
class Cell:
    fan_in: int
    depth: int
    heterogeneity: float
    menu_size: int
    epsilon: float
    width: int
    replicate: int


def _cell_seed(seed: int, cell: Cell) -> int:
    payload = f"{seed}|{cell}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def generate_problem(cell: Cell, *, seed: int) -> EpisodeProblem:
    """Generate a layered DAG. fan_in=1 is a tree; >1 creates reconvergence."""
    if cell.depth < 1 or cell.width < 1 or cell.menu_size < 2 or cell.fan_in < 1:
        raise ValueError("depth/width/fan_in must be >=1 and menu_size >=2")
    rng = random.Random(_cell_seed(seed, cell))
    layers = [["root"]]
    for level in range(1, cell.depth + 1):
        layers.append([f"l{level}n{i}" for i in range(cell.width)])
    edges: list[EdgeRecord] = []
    for level in range(1, len(layers)):
        previous, current = layers[level - 1], layers[level]
        for index, target in enumerate(current):
            if len(previous) == 1:
                parents = previous
            else:
                count = min(cell.fan_in, len(previous))
                primary = previous[index % len(previous)]
                rest = [node for node in previous if node != primary]
                parents = [primary, *rng.sample(rest, count - 1)]
            for source in parents:
                scale = 10 ** rng.uniform(-cell.heterogeneity, cell.heterogeneity)
                options = []
                # More expensive menu entries monotonically lower the declared
                # miss bound; mild jitter makes non-uniform allocation useful.
                for menu_index in range(cell.menu_size):
                    strength = menu_index / (cell.menu_size - 1)
                    delta = (
                        0.0
                        if menu_index == cell.menu_size - 1
                        else min(0.95, (0.12 * scale) * (0.12 ** strength))
                    )
                    cost = (1.0 + 12.0 * strength * strength) / scale
                    options.append(OptionRecord(
                        name=f"m{menu_index}", cost=round(cost, 8), delta=round(delta, 10)
                    ))
                edges.append(EdgeRecord(source, target, tuple(options)))
    topology = "tree" if cell.fan_in == 1 else "dag"
    if cell.width == 1:
        topology = "chain"
    return EpisodeProblem(
        episode_id=f"synthetic-r{cell.replicate}",
        edges=edges,
        roots=["root"],
        targets=layers[-1],
        topology=topology,
        epsilon=cell.epsilon,
        contract_source={
            "method": "synthetic-declared",
            "seed": seed,
            "heterogeneity": cell.heterogeneity,
        },
    )


def evaluate_cell(
    cell: Cell,
    *,
    seed: int,
    exact_max_edges: int,
    core=None,
) -> dict[str, Any]:
    core = core or _load_core()
    problem = generate_problem(cell, seed=seed)
    core_problem = to_core_problem(problem, core)
    combinations = math.prod(len(edge.options) for edge in problem.edges)
    small = len(problem.edges) <= exact_max_edges and combinations <= 1_000_000
    solver = (
        "chain" if problem.topology == "chain" else
        "tree" if problem.topology == "tree" else
        ("milp" if small else "heuristic")
    )
    try:
        solved = _call_solver(core, solver, core_problem)
    except (AttributeError, NotImplementedError, RuntimeError):
        if solver != "milp":
            raise
        solver = "heuristic"
        solved = _call_solver(core, solver, core_problem)
    oracle = _call_solver(core, "brute_force", core_problem) if small else None
    cost = _result_field(solved, "cost", "total_cost")
    oracle_cost = _result_field(oracle, "cost", "total_cost") if oracle is not None else None
    lower_bound = sum(min(option.cost for option in edge.options) for edge in problem.edges)
    exact_gap = (
        (float(cost) - float(oracle_cost)) / max(abs(float(oracle_cost)), 1e-12)
        if cost is not None and oracle_cost is not None
        else None
    )
    lower_bound_gap = (
        (float(cost) - lower_bound) / lower_bound
        if cost is not None and lower_bound > 0
        else (0.0 if cost == 0 and lower_bound == 0 else None)
    )
    return {
        "cell": {
            "fan_in": cell.fan_in,
            "reconvergence": cell.fan_in > 1,
            "depth": cell.depth,
            "heterogeneity": cell.heterogeneity,
            "menu_size": cell.menu_size,
            "epsilon": cell.epsilon,
            "width": cell.width,
            "replicate": cell.replicate,
        },
        "seed": _cell_seed(seed, cell),
        "topology": problem.topology,
        "solver": solver,
        "n_edges": len(problem.edges),
        "n_combinations": combinations,
        "cost": cost,
        "feasible": _result_field(solved, "feasible"),
        "max_path_risk": _result_field(solved, "max_path_risk", "risk"),
        "gap": exact_gap if exact_gap is not None else lower_bound_gap,
        "gap_kind": "exact-opt" if exact_gap is not None else "cheapest-menu-lower-bound",
        "lower_bound": oracle_cost if oracle_cost is not None else lower_bound,
        "result": _jsonable(solved),
        "brute_force": _jsonable(oracle) if oracle is not None else None,
    }


def _product(
    fan_ins: Iterable[int],
    depths: Iterable[int],
    heterogeneities: Iterable[float],
    menu_sizes: Iterable[int],
    epsilons: Iterable[float],
    widths: Iterable[int],
    replicates: int,
) -> Iterable[Cell]:
    for fan_in in fan_ins:
        for depth in depths:
            for heterogeneity in heterogeneities:
                for menu_size in menu_sizes:
                    for epsilon in epsilons:
                        for width in widths:
                            for replicate in range(replicates):
                                yield Cell(
                                    fan_in, depth, heterogeneity, menu_size,
                                    epsilon, width, replicate,
                                )


def run_grid(
    *,
    fan_ins: Iterable[int],
    depths: Iterable[int],
    heterogeneities: Iterable[float],
    menu_sizes: Iterable[int],
    epsilons: Iterable[float],
    widths: Iterable[int],
    replicates: int,
    seed: int,
    exact_max_edges: int,
    core=None,
) -> dict[str, Any]:
    cells = list(_product(
        fan_ins, depths, heterogeneities, menu_sizes, epsilons, widths, replicates
    ))
    results = [
        evaluate_cell(cell, seed=seed, exact_max_edges=exact_max_edges, core=core)
        for cell in cells
    ]
    return {
        "seed": seed,
        "exact_max_edges": exact_max_edges,
        "n_cells": len(cells),
        "contract_scope": "algorithm-only synthetic declared contracts; no empirical certification",
        "results": results,
    }


def _csv(value: str, cast):
    return [cast(item) for item in value.split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fan-in", default="1,2,4")
    parser.add_argument("--depth", default="3,6,12")
    parser.add_argument("--heterogeneity", default="0,1,2")
    parser.add_argument("--menu-size", default="3,5")
    parser.add_argument("--epsilon", default="0.03,0.05,0.1")
    parser.add_argument("--width", default="2,5")
    parser.add_argument("--replicates", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260819)
    parser.add_argument("--exact-max-edges", type=int, default=12)
    parser.add_argument("--out", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = run_grid(
        fan_ins=_csv(args.fan_in, int),
        depths=_csv(args.depth, int),
        heterogeneities=_csv(args.heterogeneity, float),
        menu_sizes=_csv(args.menu_size, int),
        epsilons=_csv(args.epsilon, float),
        widths=_csv(args.width, int),
        replicates=args.replicates,
        seed=args.seed,
        exact_max_edges=args.exact_max_edges,
    )
    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
