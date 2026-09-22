import json

import pytest

from integrations.memebench.propagation_planner_eval import (
    _observed_cost,
    build_episode_problems,
    calibrate_contracts,
    clopper_pearson_upper,
    evaluate,
    load_joined,
    split_episodes,
)
from integrations.memebench.synthetic_planner_eval import Cell, generate_problem, run_grid


def _edge(episode, source, target, stale=True):
    return {
        "episode_id": episode,
        "dependency_id": source,
        "dependent_id": target,
        "should_stale": stale,
    }


def _verdict(stale=True, j1=True, j3=False, j4=True, tok3=10, tok4=100):
    return {
        "should_stale": stale,
        "j1_rule": j1,
        "j2_cosine": 0.5,
        "j3_cheap": j3,
        "j4_costly": j4,
        "j3_tokens": tok3,
        "j4_tokens": tok4,
        "error": None,
    }


def test_cascade_uses_conditional_observed_cost_not_marginal_product():
    fresh_then_stale = _verdict(j3=False, j4=True, tok3=11, tok4=101)
    stale_short_circuit = _verdict(j3=True, j4=False, tok3=13, tok4=103)
    assert _observed_cost(fresh_then_stale, "cascade", 7) == 119
    assert _observed_cost(stale_short_circuit, "cascade", 7) == 20


def test_contract_risk_is_positive_only_but_runtime_graph_keeps_all_edges():
    rows = [
        {**_edge("ep", "r", "a"), "_verdict": _verdict(j1=False, j3=False, j4=True)},
        {**_edge("ep", "r", "a"), "_verdict": _verdict(j1=False, j3=False, j4=True)},
        {**_edge("ep", "a", "b"), "_verdict": _verdict(j1=True, j3=True, j4=True)},
        {**_edge("ep", "x", "b", False), "_verdict": _verdict(False)},
    ]
    contracts, source = calibrate_contracts(rows, method="point", recompute_cost=7)
    assert contracts["__global__"]["J1"]["delta"] == pytest.approx(2 / 3)
    assert contracts["__global__"]["cascade"]["delta"] == 0
    assert contracts["__global__"]["cascade"]["expected_cost"] == pytest.approx(
        (117 + 117 + 17 + 117) / 4
    )
    assert contracts["__global__"]["cascade"]["risk_n"] == 3
    assert contracts["__global__"]["cascade"]["cost_n"] == 4
    problems = build_episode_problems(rows, contracts, source, epsilon=0.1)
    assert len(problems[0].edges) == 3
    assert problems[0].roots == ["r", "x"]
    assert problems[0].targets == ["b"]
    assert problems[0].topology == "dag"
    # Every evaluation edge gets the calibration mean, not its own observed cost.
    assert problems[0].edges[0].options[3].cost == problems[0].edges[1].options[3].cost


def test_episode_split_is_stable_order_independent_and_disjoint():
    rows = [_edge(f"ep{i}", "r", "x") for i in range(20)]
    calibration, evaluation, manifest = split_episodes(
        rows, seed="paper-v1", calibration_fraction=0.4
    )
    reversed_result = split_episodes(
        reversed(rows), seed="paper-v1", calibration_fraction=0.4
    )
    assert manifest == reversed_result[2]
    calibration_ids = {row["episode_id"] for row in calibration}
    evaluation_ids = {row["episode_id"] for row in evaluation}
    assert calibration_ids and evaluation_ids
    assert calibration_ids.isdisjoint(evaluation_ids)
    assert all(len(item["sha256"]) == 64 for item in manifest["assignments"])


def test_cp_upper_is_one_sided_and_conservative():
    assert clopper_pearson_upper(0, 10, 0.05) == pytest.approx(
        1 - 0.05 ** (1 / 10), rel=1e-9
    )
    assert clopper_pearson_upper(10, 10) == 1


def test_cp_contracts_are_simultaneous_over_planner_menu():
    rows = [
        {**_edge(f"ep{i}", "r", "a"), "_verdict": _verdict(j1=True, j3=True)}
        for i in range(20)
    ]
    _, source = calibrate_contracts(rows, method="cp-upper", alpha=0.05)
    assert source["simultaneous_contracts"] == 4
    assert source["per_contract_alpha"] == pytest.approx(0.0125)
    assert source["multiplicity_correction"].startswith("Bonferroni")


def test_cp_contract_counts_per_edge_not_per_episode_any_miss():
    """delta is a per-edge miss rate; episodes only shrink the denominator.

    One missed edge out of 20 must not be charged as one missed episode out of
    2.  The old any-miss-per-cluster counting inflated the numerator (1 -> 1)
    and shrank the denominator (20 -> 2) at the same time.
    """
    rows = []
    for episode in ("ep-a", "ep-b"):
        for index in range(10):
            rows.append({
                **_edge(episode, f"r-{index}", f"a-{index}"),
                "_verdict": _verdict(
                    j1=not (episode == "ep-a" and index == 0),
                    j3=True,
                ),
            })
    contracts, source = calibrate_contracts(rows, method="cp-upper", alpha=0.05)
    j1 = contracts["__global__"]["J1"]
    assert j1["risk_n"] == 20
    assert j1["risk_episode_n"] == 2
    assert j1["edge_misses"] == 1
    # 1/20 with a CP upper bound stays well under the 1/2 the old code implied.
    assert j1["delta"] < 0.5
    # The bound assumes independence: the full edge count is the denominator.
    assert j1["delta"] == pytest.approx(
        clopper_pearson_upper(1, 20, source["per_contract_alpha"])
    )
    assert j1["design_effect"] >= 1.0
    assert j1["design_effect_applied"] is False


def test_design_effect_is_reported_but_not_applied():
    """Clustering must be visible in the artifact yet absent from the bound.

    Dropping the design-effect correction (2026-09-03) makes the bound assume
    independent edges.  That assumption is anti-conservative, so the ICC has to
    stay in the artifact for the assumption to be auditable.
    """

    def build(concentrated: bool):
        rows = []
        for episode in ("ep-a", "ep-b", "ep-c", "ep-d"):
            for index in range(10):
                if concentrated:
                    miss = episode == "ep-a" and index < 4
                else:
                    miss = index == 0
                rows.append({
                    **_edge(episode, f"r-{index}", f"a-{index}"),
                    "_verdict": _verdict(j1=not miss, j3=True),
                })
        return rows

    spread, _ = calibrate_contracts(build(False), method="cp-upper", alpha=0.05)
    clustered, _ = calibrate_contracts(build(True), method="cp-upper", alpha=0.05)
    # Same 4 missed edges out of 40 either way.
    assert spread["__global__"]["J1"]["edge_misses"] == 4
    assert clustered["__global__"]["J1"]["edge_misses"] == 4
    # Clustering is still measured and still visible in the artifact.
    assert (
        clustered["__global__"]["J1"]["design_effect"]
        > spread["__global__"]["J1"]["design_effect"]
    )
    # ...but it no longer moves the bound: 4/40 either way, by assumption.
    assert (
        clustered["__global__"]["J1"]["delta"]
        == pytest.approx(spread["__global__"]["J1"]["delta"])
    )
    assert clustered["__global__"]["J1"]["design_effect_applied"] is False


def test_fixture_files_run_end_to_end_without_db_or_api(tmp_path):
    fixture_edges = []
    fixture_verdicts = []
    for index in range(20):
        fixture_edges.append(_edge(f"ep{index}", "root", "leaf"))
        fixture_verdicts.append(_verdict(
            j1=index % 2 == 0, j3=index % 3 == 0, j4=True,
            tok3=10 + index, tok4=100 + index,
        ))
    edges = {"edges": fixture_edges}
    verdicts = {"verdicts": fixture_verdicts}
    edge_path, verdict_path = tmp_path / "edges.json", tmp_path / "verdicts.json"
    edge_path.write_text(json.dumps(edges), encoding="utf-8")
    verdict_path.write_text(json.dumps(verdicts), encoding="utf-8")
    joined = load_joined(edge_path, verdict_path)
    assert len(joined) == 20
    output = evaluate(
        edge_path, verdict_path, epsilon=0.2, contract_method="point",
        recompute_cost=5, brute_force_max_edges=3,
        split_seed="fixture-v1", calibration_fraction=0.5,
    )
    result = output["results"][0]
    assert result["solver"] == "chain"
    assert result["feasible"] is True
    assert result["max_path_risk"] <= 0.2
    assert result["brute_force"] is not None
    assert "held-out" in output["certification_scope"]
    assert set(output["split"]["calibration_episode_ids"]).isdisjoint(
        output["split"]["evaluation_episode_ids"]
    )
    assert output["n_calibration_rows"] + output["n_evaluation_rows"] == 20
    json.dumps(output)


def test_synthetic_grid_is_reproducible_and_routes_large_dag_to_heuristic():
    cell = Cell(fan_in=2, depth=3, heterogeneity=1, menu_size=3,
                epsilon=0.05, width=3, replicate=0)
    assert generate_problem(cell, seed=7) == generate_problem(cell, seed=7)
    output = run_grid(
        fan_ins=[2], depths=[3], heterogeneities=[1], menu_sizes=[3],
        epsilons=[0.05], widths=[3], replicates=1, seed=7,
        exact_max_edges=2,
    )
    result = output["results"][0]
    assert result["solver"] == "heuristic"
    assert result["brute_force"] is None
    assert result["feasible"] is True


def test_small_synthetic_dag_runs_oracle_and_applicable_solver():
    output = run_grid(
        fan_ins=[2], depths=[2], heterogeneities=[0], menu_sizes=[3],
        epsilons=[0.05], widths=[2], replicates=1, seed=11,
        exact_max_edges=12,
    )
    result = output["results"][0]
    assert result["brute_force"] is not None
    assert result["solver"] in {"milp", "heuristic"}  # heuristic if PuLP is absent
    assert result["gap"] >= 0
