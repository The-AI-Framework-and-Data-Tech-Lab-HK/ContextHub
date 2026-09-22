from __future__ import annotations

import json
import math

import pytest

from integrations.memebench.chronological_policy import (
    NO_ELIGIBLE_P1_POLICY,
    P1_SELECTION_GRAPH_MISS_LIMIT,
    SPLIT_SEED,
    BuildPlan,
    assert_frozen_plan_menu,
    build_plan_from_json,
    build_plan_to_json,
    case_to_episode_result,
    collapse_cases_to_episodes,
    edge_discovery_args,
    json_safe,
    policy_from_selection_cases,
    policy_manifest_hash,
    registered_build_plans,
    select_frozen_p1_policy,
    split_episodes_for_chronological,
    validate_build_plan,
)
from integrations.memebench.common import EpisodeResult, PolicyCandidate


def test_five_registered_plans_are_unique_and_exact() -> None:
    plans = registered_build_plans()
    assert list(plans) == [
        "E_economy",
        "T_current_tau",
        "B_lambda",
        "R_full_cheap",
        "R_full_verify",
    ]
    economy = plans["E_economy"]
    assert economy.tau_disamb == 0.0
    assert economy.tau_cand == 0.0
    assert economy.p_min == 0.0
    assert math.isinf(economy.lam) and economy.lam > 0
    assert economy.tau_edge is None
    assert economy.k == 5 and economy.recency is True

    current = plans["T_current_tau"]
    assert current.tau_disamb == 0.5
    assert current.tau_cand == 0.4
    assert current.tau_edge == 0.4
    assert current.lam is None
    assert current.p_min is None

    lam = plans["B_lambda"]
    assert lam.p_min == 0.0
    assert lam.lam == pytest.approx(1e-4)
    assert lam.tau_edge is None

    cheap = plans["R_full_cheap"]
    assert cheap.tau_cand == 1.0
    assert math.isinf(cheap.lam)

    verify = plans["R_full_verify"]
    assert verify.tau_disamb == 1.0
    assert verify.tau_cand == 1.0
    assert verify.lam == 0.0
    assert verify.p_min == 0.0


def test_lambda_pmin_and_tau_edge_are_mutually_exclusive() -> None:
    mixed = BuildPlan(
        name="mixed",
        tau_disamb=0.5,
        tau_cand=0.4,
        p_min=0.0,
        lam=1e-4,
        tau_edge=0.4,
        k=5,
        recency=True,
    )
    with pytest.raises(ValueError, match="forbids tau_edge"):
        validate_build_plan(mixed)
    missing_floor = BuildPlan(
        name="no-floor",
        tau_disamb=0.5,
        tau_cand=0.4,
        p_min=None,
        lam=1e-4,
        tau_edge=None,
        k=5,
        recency=True,
    )
    with pytest.raises(ValueError, match="requires p_min"):
        validate_build_plan(missing_floor)
    tau, lam = edge_discovery_args(registered_build_plans()["T_current_tau"])
    assert tau == 0.4 and lam is None
    tau, lam = edge_discovery_args(registered_build_plans()["B_lambda"])
    assert tau == 0.0 and lam == pytest.approx(1e-4)
    tau, lam = edge_discovery_args(registered_build_plans()["E_economy"])
    assert tau == 0.0 and math.isinf(lam)


def test_infinity_json_round_trip() -> None:
    plan = registered_build_plans()["E_economy"]
    payload = build_plan_to_json(plan)
    dumped = json.dumps(payload)
    assert "Infinity" in dumped
    assert "NaN" not in dumped
    restored = build_plan_from_json(json.loads(dumped))
    assert math.isinf(restored.lam)
    assert restored == plan
    assert json_safe(float("inf")) == "Infinity"
    with pytest.raises(ValueError, match="NaN"):
        json_safe(float("nan"))


def test_split_is_order_independent_and_episode_atomic() -> None:
    ids = [f"ep-{i:03d}" for i in range(40)]
    forward = split_episodes_for_chronological(ids, seed=SPLIT_SEED)
    reverse = split_episodes_for_chronological(reversed(ids), seed=SPLIT_SEED)
    assert forward == reverse
    assert set(forward.selection_ids).isdisjoint(forward.certification_ids)
    assert set(forward.selection_ids) | set(forward.certification_ids) == set(ids)


def test_evaluation_cannot_add_or_retune_plans() -> None:
    with pytest.raises(ValueError, match="cannot add P1 plans"):
        assert_frozen_plan_menu(list(registered_build_plans()) + ["S_secret"])
    ids = [f"ep-{i:03d}" for i in range(20)]
    split = split_episodes_for_chronological(ids, seed="eval-freeze")
    policies = []
    for name, plan in registered_build_plans().items():
        cases = [
            {
                "episode_id": episode_id,
                "p1": {
                    "n_gold": 1,
                    "n_pred": 1,
                    "n_tp": 1,
                    "cheap_tokens": 10,
                    "strong_tokens": 0,
                    "cheap_none": 0,
                },
            }
            for episode_id in ids
        ]
        policies.append(policy_from_selection_cases(plan, cases))
    selected = select_frozen_p1_policy(policies, split)
    assert selected["frozen"] is True
    cert_only = [
        PolicyCandidate(
            policy_id="E_economy",
            source="tamper",
            parameters=build_plan_to_json(registered_build_plans()["E_economy"]),
            episodes=tuple(
                EpisodeResult(
                    episode_id=episode_id,
                    n_gold=1,
                    n_pred=1,
                    n_tp=0 if episode_id in split.certification_ids else 1,
                    precision=0.0 if episode_id in split.certification_ids else 1.0,
                    recall=0.0 if episode_id in split.certification_ids else 1.0,
                    cheap_tokens=1.0,
                    strong_tokens=0.0,
                )
                for episode_id in ids
            ),
        )
    ]
    # Completing the menu with the other frozen plans, all cheap, still cannot
    # use certification misses to pick a different plan.
    for name, plan in registered_build_plans().items():
        if name == "E_economy":
            continue
        cert_only.append(
            PolicyCandidate(
                policy_id=name,
                source="tamper",
                parameters=build_plan_to_json(plan),
                episodes=tuple(
                    EpisodeResult(
                        episode_id=episode_id,
                        n_gold=1,
                        n_pred=1,
                        n_tp=1,
                        precision=1.0,
                        recall=1.0,
                        cheap_tokens=50.0,
                        strong_tokens=0.0,
                    )
                    for episode_id in ids
                ),
            )
        )
    tampered = select_frozen_p1_policy(cert_only, split)
    assert tampered["selected_policy"]["policy_id"] == "E_economy"


def test_ineligible_menu_does_not_relax_threshold() -> None:
    ids = [f"ep-{i:03d}" for i in range(20)]
    split = split_episodes_for_chronological(ids, seed="no-go")
    policies = []
    for name, plan in registered_build_plans().items():
        policies.append(
            PolicyCandidate(
                policy_id=name,
                source="synthetic",
                parameters=build_plan_to_json(plan),
                episodes=tuple(
                    EpisodeResult(
                        episode_id=episode_id,
                        n_gold=1,
                        n_pred=1,
                        n_tp=0,
                        precision=0.0,
                        recall=0.0,
                        cheap_tokens=1.0,
                        strong_tokens=0.0,
                    )
                    for episode_id in ids
                ),
            )
        )
    decision = select_frozen_p1_policy(policies, split)
    assert decision["decision"] == NO_ELIGIBLE_P1_POLICY
    assert decision["frozen"] is False
    assert decision["selection_risk_limit"] == P1_SELECTION_GRAPH_MISS_LIMIT
    assert decision["smoke_fallback_plan"] == "T_current_tau"


def test_edge_miss_is_reported_but_does_not_select_policy() -> None:
    ids = [f"ep-{i:03d}" for i in range(20)]
    split = split_episodes_for_chronological(ids, seed="edge-diag")
    policies = []
    for name, plan in registered_build_plans().items():
        n_tp = 99 if name == "T_current_tau" else 20
        policies.append(
            PolicyCandidate(
                policy_id=name,
                source="synthetic",
                parameters=build_plan_to_json(plan),
                episodes=tuple(
                    EpisodeResult(
                        episode_id=episode_id,
                        n_gold=100,
                        n_pred=n_tp,
                        n_tp=n_tp,
                        precision=n_tp / 100,
                        recall=n_tp / 100,
                        cheap_tokens=1.0,
                        strong_tokens=0.0,
                    )
                    for episode_id in ids
                ),
            )
        )
    decision = select_frozen_p1_policy(policies, split)
    assert decision["frozen"] is False
    by_id = {row["policy_id"]: row for row in decision["policy_menu"]}
    assert by_id["T_current_tau"]["selection_graph_miss_rate"] == 1.0
    assert by_id["E_economy"]["selection_graph_miss_rate"] == 1.0
    assert by_id["T_current_tau"]["selection_edge_miss_rate"] == 0.01
    assert by_id["E_economy"]["selection_edge_miss_rate"] == 0.80


def test_manifest_hash_is_stable() -> None:
    assert policy_manifest_hash() == policy_manifest_hash()
    assert len(policy_manifest_hash()) == 64


def test_failed_selection_case_is_a_graph_miss_not_a_hit() -> None:
    failed = case_to_episode_result(
        {
            "episode_id": "ep-fail",
            "errors": ["Timeout"],
            "p1": {"n_gold": 0, "n_pred": 0, "n_tp": 0, "scored": False},
        }
    )
    assert failed.graph_miss is True
    assert failed.n_gold >= 1
    assert failed.n_tp == 0


def test_multi_case_episode_collapses_to_one_miss() -> None:
    cases = [
        {
            "episode_id": "ep-1",
            "p1": {"n_gold": 1, "n_pred": 1, "n_tp": 1, "scored": True, "cheap_none": 0},
        },
        {
            "episode_id": "ep-1",
            "p1": {"n_gold": 1, "n_pred": 1, "n_tp": 0, "scored": True, "cheap_none": 0},
        },
        {
            "episode_id": "ep-2",
            "p1": {"n_gold": 1, "n_pred": 1, "n_tp": 1, "scored": True, "cheap_none": 0},
        },
    ]
    episodes = collapse_cases_to_episodes(cases)
    by_id = {item.episode_id: item for item in episodes}
    assert len(episodes) == 2
    assert by_id["ep-1"].graph_miss is True
    assert by_id["ep-2"].graph_miss is False


def test_failed_cheap_policy_is_not_selected() -> None:
    ids = [f"ep-{i:03d}" for i in range(20)]
    split = split_episodes_for_chronological(ids, seed="fail-cheap")
    policies = []
    for name, plan in registered_build_plans().items():
        if name == "E_economy":
            cases = [
                {
                    "episode_id": episode_id,
                    "errors": ["boom"],
                    "p1": {
                        "n_gold": 0,
                        "n_pred": 0,
                        "n_tp": 0,
                        "scored": False,
                        "cheap_tokens": 0,
                        "strong_tokens": 0,
                    },
                }
                for episode_id in ids
            ]
        else:
            cases = [
                {
                    "episode_id": episode_id,
                    "p1": {
                        "n_gold": 1,
                        "n_pred": 1,
                        "n_tp": 1,
                        "scored": True,
                        "cheap_tokens": 50,
                        "strong_tokens": 0,
                        "cheap_none": 0,
                    },
                }
                for episode_id in ids
            ]
        policies.append(policy_from_selection_cases(plan, cases))
    decision = select_frozen_p1_policy(policies, split)
    assert decision["frozen"] is True
    assert decision["selected_policy"]["policy_id"] != "E_economy"
    economy = next(
        row for row in decision["policy_menu"] if row["policy_id"] == "E_economy"
    )
    assert economy["selection_graph_miss_rate"] == 1.0
    assert economy["selection_risk_ok"] is False
