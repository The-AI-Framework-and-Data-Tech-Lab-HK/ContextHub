"""Exact, baseline, and heuristic planners for propagation DAGs."""

from __future__ import annotations

from collections.abc import Mapping
import itertools
import math
from typing import Hashable

from .models import (
    EdgeKey,
    GraphProblem,
    PlanResult,
    PlanningInfeasibleError,
    PolicyCandidate,
)

_TOLERANCE = 1e-12


def _relevant_edges(problem: GraphProblem) -> set[EdgeKey]:
    children: dict[Hashable, list[Hashable]] = {node: [] for node in problem.nodes}
    parents: dict[Hashable, list[Hashable]] = {node: [] for node in problem.nodes}
    for edge in problem.edges:
        children[edge.upstream].append(edge.dependent)
        parents[edge.dependent].append(edge.upstream)

    forward = set(problem.sources)
    for node in problem.topological_order:
        if node in forward:
            forward.update(children[node])
    backward = set(problem.targets)
    for node in reversed(problem.topological_order):
        if node in backward:
            backward.update(parents[node])
    relevant_nodes = forward & backward
    return {
        edge.key
        for edge in problem.edges
        if edge.upstream in relevant_nodes and edge.dependent in relevant_nodes
    }


def _validate_assignments(
    problem: GraphProblem,
    assignments: Mapping[EdgeKey, PolicyCandidate],
) -> None:
    expected = {edge.key for edge in problem.edges}
    if set(assignments) != expected:
        missing = expected - set(assignments)
        extra = set(assignments) - expected
        raise ValueError(f"assignment edge mismatch (missing={missing!r}, extra={extra!r})")
    for edge in problem.edges:
        if assignments[edge.key] not in edge.candidates:
            raise ValueError(f"candidate is not in menu for edge {edge.key!r}")


def maximum_path_risk(
    problem: GraphProblem,
    assignments: Mapping[EdgeKey, PolicyCandidate],
) -> tuple[float, tuple[Hashable, ...]]:
    """Return the maximum risk and a maximizing source-to-target path."""

    _validate_assignments(problem, assignments)
    distance = {node: -math.inf for node in problem.nodes}
    predecessor: dict[Hashable, Hashable] = {}
    for source in problem.sources:
        distance[source] = 0.0

    outgoing: dict[Hashable, list[Hashable]] = {node: [] for node in problem.nodes}
    for edge in problem.edges:
        outgoing[edge.upstream].append(edge.dependent)
    for node in problem.topological_order:
        if distance[node] == -math.inf:
            continue
        for child in outgoing[node]:
            candidate_risk = distance[node] + assignments[(node, child)].delta
            if candidate_risk > distance[child]:
                distance[child] = candidate_risk
                predecessor[child] = node

    target = max(problem.targets, key=lambda node: distance[node])
    risk = distance[target]
    path = [target]
    while path[-1] not in problem.sources:
        parent = predecessor.get(path[-1])
        if parent is None:  # GraphProblem already checks target reachability.
            raise RuntimeError("failed to reconstruct a protected path")
        path.append(parent)
    path.reverse()
    return risk, tuple(path)


def validate_plan(problem: GraphProblem, result: PlanResult) -> bool:
    """Recompute all cost and path-risk metadata; return global feasibility."""

    _validate_assignments(problem, result.assignments)
    total_cost = sum(candidate.cost for candidate in result.assignments.values())
    risk, _ = maximum_path_risk(problem, result.assignments)
    if not math.isclose(total_cost, result.total_cost, rel_tol=1e-10, abs_tol=1e-10):
        raise ValueError("reported total_cost does not match assignments")
    if not math.isclose(risk, result.max_path_risk, rel_tol=1e-10, abs_tol=1e-10):
        raise ValueError("reported max_path_risk does not match assignments")
    feasible = risk <= problem.epsilon + _TOLERANCE
    if feasible != result.feasible:
        raise ValueError("reported feasible flag does not match all-path validation")
    return feasible


def _make_result(
    problem: GraphProblem,
    assignments: Mapping[EdgeKey, PolicyCandidate],
    method: str,
) -> PlanResult:
    risk, path = maximum_path_risk(problem, assignments)
    feasible = risk <= problem.epsilon + _TOLERANCE
    result = PlanResult(
        assignments=assignments,
        total_cost=sum(candidate.cost for candidate in assignments.values()),
        max_path_risk=risk,
        feasible=feasible,
        method=method,
        violating_path=None if feasible else path,
    )
    validate_plan(problem, result)
    return result


def _raise_if_infeasible(result: PlanResult) -> PlanResult:
    if not result.feasible:
        raise PlanningInfeasibleError(
            f"no all-path feasible plan for epsilon; maximum risk is "
            f"{result.max_path_risk:g} on {result.violating_path!r}"
        )
    return result


def brute_force_plan(
    problem: GraphProblem,
    *,
    max_combinations: int = 1_000_000,
) -> PlanResult:
    """Find the exact optimum by enumerating every finite edge menu."""

    combinations = math.prod(len(edge.candidates) for edge in problem.edges)
    if combinations > max_combinations:
        raise ValueError(
            f"brute-force search has {combinations} combinations, above "
            f"max_combinations={max_combinations}"
        )
    best: PlanResult | None = None
    for choices in itertools.product(*(edge.candidates for edge in problem.edges)):
        assignments = {edge.key: choice for edge, choice in zip(problem.edges, choices)}
        result = _make_result(problem, assignments, "brute-force")
        if result.feasible and (
            best is None or result.total_cost < best.total_cost - _TOLERANCE
        ):
            best = result
    if best is None:
        raise PlanningInfeasibleError("no assignment satisfies every protected path")
    return best


def _maximum_protected_path_length(problem: GraphProblem) -> int:
    lengths = {node: -math.inf for node in problem.nodes}
    for source in problem.sources:
        lengths[source] = 0
    outgoing: dict[Hashable, list[Hashable]] = {node: [] for node in problem.nodes}
    for edge in problem.edges:
        outgoing[edge.upstream].append(edge.dependent)
    for node in problem.topological_order:
        if lengths[node] == -math.inf:
            continue
        for child in outgoing[node]:
            lengths[child] = max(lengths[child], lengths[node] + 1)
    return int(max(lengths[target] for target in problem.targets))


def _cheapest_under(edge, threshold: float) -> PolicyCandidate:
    eligible = [
        candidate
        for candidate in edge.candidates
        if candidate.delta <= threshold + _TOLERANCE
    ]
    if not eligible:
        raise PlanningInfeasibleError(
            f"edge {edge.key!r} has no candidate with delta <= {threshold:g}"
        )
    return min(eligible, key=lambda candidate: (candidate.cost, candidate.delta, candidate.name))


def uniform_epsilon_plan(problem: GraphProblem) -> PlanResult:
    """Baseline that fixes every relevant edge's budget to ``epsilon / L``."""

    relevant = _relevant_edges(problem)
    length = _maximum_protected_path_length(problem)
    threshold = math.inf if length == 0 else problem.epsilon / length
    assignments = {
        edge.key: (
            _cheapest_under(edge, threshold)
            if edge.key in relevant
            else min(edge.candidates, key=lambda candidate: (candidate.cost, candidate.delta))
        )
        for edge in problem.edges
    }
    return _raise_if_infeasible(_make_result(problem, assignments, "uniform-epsilon/L"))


def independent_edge_plan(problem: GraphProblem) -> PlanResult:
    """Baseline that gives each edge the full epsilon independently.

    The returned result is still checked against every complete path.  Thus
    ``feasible`` can be false, exposing the baseline's missing joint budget
    constraint instead of silently presenting it as a certified plan.
    """

    relevant = _relevant_edges(problem)
    assignments = {
        edge.key: (
            _cheapest_under(edge, problem.epsilon)
            if edge.key in relevant
            else min(edge.candidates, key=lambda candidate: (candidate.cost, candidate.delta))
        )
        for edge in problem.edges
    }
    return _make_result(problem, assignments, "independent-per-edge")


def _risk_units(delta: float, epsilon: float, grid_size: int) -> int:
    if delta == 0:
        return 0
    if epsilon == 0:
        return grid_size + 1
    step = epsilon / grid_size
    return math.ceil(delta / step)


def _validate_grid_size(grid_size: int) -> None:
    if isinstance(grid_size, bool) or not isinstance(grid_size, int):
        raise TypeError("grid_size must be an integer")
    if grid_size <= 0:
        raise ValueError("grid_size must be positive")


def chain_risk_grid_plan(problem: GraphProblem, *, grid_size: int = 1000) -> PlanResult:
    """Solve a single protected chain exactly after conservative risk rounding."""

    _validate_grid_size(grid_size)
    relevant = _relevant_edges(problem)
    relevant_options = [edge for edge in problem.edges if edge.key in relevant]
    indegree = {node: 0 for node in problem.nodes}
    outdegree = {node: 0 for node in problem.nodes}
    for edge in relevant_options:
        indegree[edge.dependent] += 1
        outdegree[edge.upstream] += 1
    if (
        len(problem.sources) != 1
        or len(problem.targets) != 1
        or any(indegree[node] > 1 or outdegree[node] > 1 for node in problem.nodes)
    ):
        raise ValueError("chain planner requires one source, one target, and no branching")

    source = problem.sources[0]
    edge_by_parent = {edge.upstream: edge for edge in relevant_options}
    chain = []
    node = source
    while node != problem.targets[0]:
        edge = edge_by_parent.get(node)
        if edge is None:
            raise ValueError("protected subgraph is not a contiguous chain")
        chain.append(edge)
        node = edge.dependent

    infinity = math.inf
    costs = [infinity] * (grid_size + 1)
    costs[0] = 0.0
    backpointers: list[list[tuple[int, PolicyCandidate] | None]] = []
    for edge in chain:
        next_costs = [infinity] * (grid_size + 1)
        pointers: list[tuple[int, PolicyCandidate] | None] = [None] * (grid_size + 1)
        for used, prior_cost in enumerate(costs):
            if prior_cost == infinity:
                continue
            for candidate in edge.candidates:
                units = _risk_units(candidate.delta, problem.epsilon, grid_size)
                new_used = used + units
                new_cost = prior_cost + candidate.cost
                if new_used <= grid_size and new_cost < next_costs[new_used] - _TOLERANCE:
                    next_costs[new_used] = new_cost
                    pointers[new_used] = (used, candidate)
        costs = next_costs
        backpointers.append(pointers)

    best_units = min(range(grid_size + 1), key=lambda units: costs[units])
    if costs[best_units] == infinity:
        raise PlanningInfeasibleError("no chain plan fits the rounded risk budget")
    assignments: dict[EdgeKey, PolicyCandidate] = {}
    used = best_units
    for edge, pointers in reversed(list(zip(chain, backpointers))):
        pointer = pointers[used]
        if pointer is None:
            raise RuntimeError("chain DP reconstruction failed")
        previous, candidate = pointer
        assignments[edge.key] = candidate
        used = previous
    for edge in problem.edges:
        if edge.key not in assignments:
            assignments[edge.key] = min(
                edge.candidates, key=lambda candidate: (candidate.cost, candidate.delta)
            )
    return _raise_if_infeasible(_make_result(problem, assignments, "chain-risk-grid-dp"))


def tree_risk_grid_plan(problem: GraphProblem, *, grid_size: int = 1000) -> PlanResult:
    """Tree DP with one per-path budget independently shared by every branch."""

    _validate_grid_size(grid_size)
    relevant = _relevant_edges(problem)
    relevant_options = [edge for edge in problem.edges if edge.key in relevant]
    indegree = {node: 0 for node in problem.nodes}
    children = {node: [] for node in problem.nodes}
    for edge in relevant_options:
        indegree[edge.dependent] += 1
        children[edge.upstream].append(edge)
    if any(indegree[node] > 1 for node in problem.nodes):
        raise ValueError("tree planner does not allow reconvergent nodes")
    if any(indegree[source] != 0 for source in problem.sources):
        raise ValueError("tree planner requires each source to be a forest root")

    memo: dict[tuple[Hashable, int], float] = {}
    choices: dict[tuple[Hashable, int, EdgeKey], tuple[PolicyCandidate, int]] = {}

    def solve(node: Hashable, budget: int) -> float:
        key = (node, budget)
        if key in memo:
            return memo[key]
        total = 0.0
        for edge in children[node]:
            best_cost = math.inf
            best: tuple[PolicyCandidate, int] | None = None
            for candidate in edge.candidates:
                units = _risk_units(candidate.delta, problem.epsilon, grid_size)
                if units > budget:
                    continue
                child_cost = solve(edge.dependent, budget - units)
                cost = candidate.cost + child_cost
                if cost < best_cost - _TOLERANCE:
                    best_cost = cost
                    best = (candidate, budget - units)
            if best is None:
                memo[key] = math.inf
                return math.inf
            choices[(node, budget, edge.key)] = best
            total += best_cost
        memo[key] = total
        return total

    root_costs = [solve(source, grid_size) for source in problem.sources]
    if any(cost == math.inf for cost in root_costs):
        raise PlanningInfeasibleError("no tree plan fits the rounded per-path risk budget")

    assignments: dict[EdgeKey, PolicyCandidate] = {}

    def reconstruct(node: Hashable, budget: int) -> None:
        for edge in children[node]:
            candidate, remaining = choices[(node, budget, edge.key)]
            assignments[edge.key] = candidate
            reconstruct(edge.dependent, remaining)

    for source in problem.sources:
        reconstruct(source, grid_size)
    for edge in problem.edges:
        if edge.key not in assignments:
            assignments[edge.key] = min(
                edge.candidates, key=lambda candidate: (candidate.cost, candidate.delta)
            )
    return _raise_if_infeasible(_make_result(problem, assignments, "tree-risk-grid-dp"))


def dag_milp_plan(
    problem: GraphProblem,
    *,
    time_limit: float | None = None,
    solver_message: bool = False,
) -> PlanResult:
    """Solve the conservative all-path DAG formulation exactly with PuLP."""

    try:
        import pulp  # type: ignore[import-not-found]
    except (ImportError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            "dag_milp_plan requires PuLP; install the 'research' extra "
            "(pip install 'contexthub[research]')"
        ) from exc

    relevant = _relevant_edges(problem)
    model = pulp.LpProblem("contexthub_all_path_planner", pulp.LpMinimize)
    variables = {
        (edge.key, index): pulp.LpVariable(
            f"x_{edge_index}_{index}", lowBound=0, upBound=1, cat=pulp.LpBinary
        )
        for edge_index, edge in enumerate(problem.edges)
        for index, _ in enumerate(edge.candidates)
    }
    model += pulp.lpSum(
        candidate.cost * variables[(edge.key, index)]
        for edge in problem.edges
        for index, candidate in enumerate(edge.candidates)
    )
    for edge in problem.edges:
        model += (
            pulp.lpSum(
                variables[(edge.key, index)] for index in range(len(edge.candidates))
            )
            == 1
        )

    relevant_nodes = {
        endpoint for edge_key in relevant for endpoint in edge_key
    } | set(problem.sources) | set(problem.targets)
    potential = {
        node: pulp.LpVariable(
            f"risk_{index}", lowBound=0, upBound=problem.epsilon, cat=pulp.LpContinuous
        )
        for index, node in enumerate(problem.nodes)
        if node in relevant_nodes
    }
    for edge in problem.edges:
        if edge.key not in relevant:
            continue
        selected_risk = pulp.lpSum(
            candidate.delta * variables[(edge.key, index)]
            for index, candidate in enumerate(edge.candidates)
        )
        model += potential[edge.dependent] >= potential[edge.upstream] + selected_risk
    for target in problem.targets:
        model += potential[target] <= problem.epsilon

    solver = pulp.PULP_CBC_CMD(msg=solver_message, timeLimit=time_limit)
    status = model.solve(solver)
    if status == pulp.LpStatusInfeasible:
        raise PlanningInfeasibleError("MILP reports that the all-path problem is infeasible")
    if status != pulp.LpStatusOptimal:
        raise RuntimeError(f"MILP did not prove optimality (status={pulp.LpStatus[status]})")

    assignments = {}
    for edge in problem.edges:
        selected = [
            candidate
            for index, candidate in enumerate(edge.candidates)
            if pulp.value(variables[(edge.key, index)]) > 0.5
        ]
        if len(selected) != 1:
            raise RuntimeError(f"MILP returned no unique policy for edge {edge.key!r}")
        assignments[edge.key] = selected[0]
    return _raise_if_infeasible(_make_result(problem, assignments, "dag-all-path-milp"))


def repair_large_dag_plan(problem: GraphProblem) -> PlanResult:
    """Build a feasible large-DAG plan by monotone risk upgrades.

    The initial assignment is independently cheapest.  Each iteration finds a
    maximum-risk violating path and replaces one policy on that path by a
    strictly lower-risk menu entry.  Finite menus with a zero-risk fallback on
    every relevant edge therefore guarantee termination.
    """

    assignments = {
        edge.key: min(
            edge.candidates,
            key=lambda candidate: (candidate.cost, candidate.delta, candidate.name),
        )
        for edge in problem.edges
    }
    edge_map = problem.edge_map
    while True:
        risk, path = maximum_path_risk(problem, assignments)
        if risk <= problem.epsilon + _TOLERANCE:
            return _make_result(problem, assignments, "large-dag-monotone-repair")

        upgrades: list[
            tuple[float, float, float, str, EdgeKey, PolicyCandidate]
        ] = []
        for upstream, dependent in zip(path, path[1:]):
            key = (upstream, dependent)
            current = assignments[key]
            for candidate in edge_map[key].candidates:
                reduction = current.delta - candidate.delta
                if reduction <= _TOLERANCE:
                    continue
                added_cost = candidate.cost - current.cost
                score = added_cost / reduction
                upgrades.append(
                    (score, candidate.cost, candidate.delta, candidate.name, key, candidate)
                )
        if not upgrades:
            result = _make_result(problem, assignments, "large-dag-monotone-repair")
            raise PlanningInfeasibleError(
                f"violating path {result.violating_path!r} has no lower-risk upgrade"
            )
        _, _, _, _, key, candidate = min(upgrades)
        assignments[key] = candidate

