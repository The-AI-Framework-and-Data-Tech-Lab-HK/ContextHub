from __future__ import annotations

import math

import pytest

from contexthub.planning import (
    EdgeOption,
    GraphProblem,
    PlanningInfeasibleError,
    PolicyCandidate,
    brute_force_plan,
    chain_risk_grid_plan,
    clopper_pearson_upper,
    dag_milp_plan,
    independent_edge_plan,
    repair_large_dag_plan,
    tree_risk_grid_plan,
    uniform_epsilon_plan,
    validate_plan,
)
from contexthub.planning.statistics import _binomial_cdf


def policy(name: str, cost: float, delta: float) -> PolicyCandidate:
    return PolicyCandidate(name=name, cost=cost, delta=delta)


def edge(
    upstream: str,
    dependent: str,
    *candidates: PolicyCandidate,
) -> EdgeOption:
    return EdgeOption(upstream, dependent, candidates)


def problem(
    nodes: tuple[str, ...],
    edges: tuple[EdgeOption, ...],
    epsilon: float,
    *,
    sources: tuple[str, ...] = ("s",),
    targets: tuple[str, ...] = ("t",),
) -> GraphProblem:
    return GraphProblem(nodes, edges, sources, targets, epsilon)


def test_models_strictly_validate_graph_and_menus() -> None:
    cheap = policy("cheap", 0, 0.1)
    with pytest.raises(ValueError, match="non-negative"):
        policy("bad", -1, 0)
    with pytest.raises(ValueError, match="non-negative"):
        policy("bad", 0, math.nan)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        policy("bad", 0, 1.01)
    with pytest.raises(ValueError, match="at least one"):
        edge("s", "t")
    with pytest.raises(ValueError, match="unique"):
        edge("s", "t", cheap, cheap)
    with pytest.raises(ValueError, match="acyclic"):
        problem(
            ("s", "a", "t"),
            (
                edge("s", "a", cheap),
                edge("a", "t", cheap),
                edge("t", "s", cheap),
            ),
            1,
        )
    with pytest.raises(ValueError, match="epsilon"):
        problem(("s", "t"), (edge("s", "t", cheap),), -0.1)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        problem(("s", "t"), (edge("s", "t", cheap),), 1.01)


def test_chain_grid_dp_matches_brute_force_optimum() -> None:
    graph = problem(
        ("s", "a", "t"),
        (
            edge("s", "a", policy("cheap", 0, 0.07), policy("safe", 1, 0)),
            edge("a", "t", policy("cheap", 0, 0.07), policy("safe", 3, 0)),
        ),
        0.1,
    )
    exact = brute_force_plan(graph)
    dynamic = chain_risk_grid_plan(graph, grid_size=100)
    assert exact.total_cost == dynamic.total_cost == 1
    assert validate_plan(graph, dynamic)


def test_chain_grid_rounds_decimal_risk_upward() -> None:
    graph = problem(
        ("s", "a", "b", "t"),
        (
            edge("s", "a", policy("cheap", 0, 0.101), policy("zero", 1, 0)),
            edge("a", "b", policy("cheap", 0, 0.101), policy("zero", 1, 0)),
            edge("b", "t", policy("cheap", 0, 0.101), policy("zero", 1, 0)),
        ),
        0.3,
    )
    result = chain_risk_grid_plan(graph, grid_size=3)
    assert result.total_cost == 2
    assert result.max_path_risk == pytest.approx(0.101)


def test_tree_dp_reuses_same_path_budget_for_siblings() -> None:
    graph = problem(
        ("s", "left", "right"),
        (
            edge("s", "left", policy("cheap", 0, 0.06), policy("zero", 2, 0)),
            edge("s", "right", policy("cheap", 0, 0.06), policy("zero", 3, 0)),
        ),
        0.1,
        targets=("left", "right"),
    )
    exact = brute_force_plan(graph)
    dynamic = tree_risk_grid_plan(graph, grid_size=10)
    assert exact.total_cost == dynamic.total_cost == 0
    assert all(choice.name == "cheap" for choice in dynamic.assignments.values())


def test_tree_dp_matches_brute_force_and_aggregates_branch_costs() -> None:
    graph = problem(
        ("s", "a", "left", "right"),
        (
            edge("s", "a", policy("cheap", 0, 0.06), policy("zero", 5, 0)),
            edge("a", "left", policy("cheap", 0, 0.06), policy("zero", 1, 0)),
            edge("a", "right", policy("cheap", 0, 0.06), policy("zero", 2, 0)),
        ),
        0.1,
        targets=("left", "right"),
    )
    exact = brute_force_plan(graph)
    dynamic = tree_risk_grid_plan(graph, grid_size=10)
    assert exact.total_cost == dynamic.total_cost == 3
    assert validate_plan(graph, dynamic)


def test_uniform_and_independent_baseline_constraint_behavior() -> None:
    graph = problem(
        ("s", "a", "t"),
        (
            edge("s", "a", policy("local", 0, 0.06), policy("safe", 1, 0.04)),
            edge("a", "t", policy("local", 0, 0.06), policy("safe", 1, 0.04)),
        ),
        0.1,
    )
    independent = independent_edge_plan(graph)
    uniform = uniform_epsilon_plan(graph)
    assert not independent.feasible
    assert independent.max_path_risk == pytest.approx(0.12)
    assert uniform.feasible
    assert uniform.total_cost == 2


def test_exact_planners_report_infeasible_without_relaxing_epsilon() -> None:
    graph = problem(
        ("s", "a", "t"),
        (
            edge("s", "a", policy("only", 0, 0.06)),
            edge("a", "t", policy("only", 0, 0.06)),
        ),
        0.1,
    )
    with pytest.raises(PlanningInfeasibleError):
        brute_force_plan(graph)
    with pytest.raises(PlanningInfeasibleError):
        chain_risk_grid_plan(graph, grid_size=100)
    with pytest.raises(PlanningInfeasibleError):
        tree_risk_grid_plan(graph, grid_size=100)


def reconvergent_graph(k: int, epsilon: float = 0.1) -> GraphProblem:
    nodes = ("s", *(f"u{i}" for i in range(k)), "t")
    edges: list[EdgeOption] = []
    fixed = policy("fixed", 0, 0)
    for index in range(k):
        middle = f"u{index}"
        edges.append(edge("s", middle, fixed))
        edges.append(
            edge(
                middle,
                "t",
                policy("cheap", 0, epsilon + 0.01),
                policy("strong", 1, 0),
            )
        )
    return problem(tuple(nodes), tuple(edges), epsilon)


def test_reconvergent_dag_milp_matches_brute_force_optimum() -> None:
    pytest.importorskip("pulp")
    graph = reconvergent_graph(3)
    exact = brute_force_plan(graph)
    milp = dag_milp_plan(graph)
    assert exact.total_cost == milp.total_cost == 3
    assert validate_plan(graph, milp)


def test_all_path_theta_k_fixture_requires_every_branch_upgrade() -> None:
    pytest.importorskip("pulp")
    for branch_count in (1, 2, 5):
        result = dag_milp_plan(reconvergent_graph(branch_count))
        assert result.total_cost == branch_count
        assert sum(
            choice.name == "strong" for choice in result.assignments.values()
        ) == branch_count


def test_milp_missing_dependency_error_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def blocked_import(name: str, *args, **kwargs):
        if name == "pulp":
            raise ModuleNotFoundError("blocked in test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(RuntimeError, match=r"research.*extra"):
        dag_milp_plan(reconvergent_graph(1))


def test_large_dag_repair_uses_zero_delta_fallback_and_checks_all_paths() -> None:
    graph = reconvergent_graph(4, epsilon=0)
    result = repair_large_dag_plan(graph)
    assert result.feasible
    assert result.max_path_risk == 0
    assert result.total_cost == 4
    assert validate_plan(graph, result)


def test_large_dag_repair_reports_path_without_lower_risk_candidate() -> None:
    graph = problem(
        ("s", "t"),
        (edge("s", "t", policy("only", 0, 0.2)),),
        0.1,
    )
    with pytest.raises(PlanningInfeasibleError, match="no lower-risk upgrade"):
        repair_large_dag_plan(graph)


def test_clopper_pearson_boundaries_and_exact_fallback() -> None:
    alpha = 0.05
    zero_failures = clopper_pearson_upper(0, 20, alpha, use_scipy=False)
    assert zero_failures == pytest.approx(1 - alpha ** (1 / 20))
    assert clopper_pearson_upper(20, 20, alpha, use_scipy=False) == 1

    upper = clopper_pearson_upper(5, 10, alpha, use_scipy=False)
    assert upper == pytest.approx(0.7775588989918709)
    assert _binomial_cdf(5, 10, upper) == pytest.approx(alpha, abs=1e-12)


def test_clopper_pearson_fallback_matches_scipy_when_available() -> None:
    pytest.importorskip("scipy")
    for failures, trials, alpha in (
        (0, 7, 0.01),
        (1, 10, 0.01),
        (17, 40, 0.01),
        (99, 100, 0.01),
        (5, 10, 0.9),
    ):
        scipy_bound = clopper_pearson_upper(failures, trials, alpha)
        fallback_bound = clopper_pearson_upper(
            failures, trials, alpha, use_scipy=False
        )
        assert fallback_bound == pytest.approx(scipy_bound, abs=2e-12)


@pytest.mark.parametrize(
    ("failures", "trials", "alpha"),
    [(-1, 10, 0.05), (11, 10, 0.05), (1, 0, 0.05), (1, 10, 0), (1, 10, 1)],
)
def test_clopper_pearson_rejects_invalid_inputs(
    failures: int,
    trials: int,
    alpha: float,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        clopper_pearson_upper(failures, trials, alpha)

