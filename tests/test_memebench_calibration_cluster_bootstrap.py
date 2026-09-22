import random

import pytest

from integrations.memebench.calibration_cluster_bootstrap import (
    OPTIONS,
    bootstrap_design_effect,
    cluster_bootstrap,
    compare_bounds,
    feasibility,
    miss_clusters,
    misses_to_flip,
    replicated_bootstrap,
)


def _row(episode, *, j3_stale):
    """One positive edge; j3_stale=False is a miss for option J3."""
    return {
        "episode_id": episode,
        "should_stale": True,
        "_verdict": {
            "should_stale": True,
            "j1_rule": True,
            "j2_cosine": 0.5,
            "j3_cheap": j3_stale,
            "j4_costly": True,
            "j3_tokens": 10,
            "j4_tokens": 100,
            "error": None,
        },
    }


def _boot(clusters, *, iters=4000, alpha=0.05, seed="t"):
    return cluster_bootstrap(
        clusters, iters=iters, alpha=alpha, rng=random.Random(seed)
    )


def _rep(clusters, *, iters=2000, replicates=4, alpha=0.05, seed="t"):
    return replicated_bootstrap(
        clusters,
        iters=iters,
        replicates=replicates,
        alpha=alpha,
        seed_prefix=seed,
    )


def test_miss_clusters_groups_by_episode_and_flags_fresh_as_miss():
    rows = [
        _row("e1", j3_stale=True),
        _row("e1", j3_stale=False),
        _row("e2", j3_stale=False),
    ]
    clusters = miss_clusters(rows, "J3")
    assert sorted(len(c) for c in clusters) == [1, 2]
    assert sum(sum(c) for c in clusters) == 2


def test_point_estimate_is_pooled_ratio_over_all_edges():
    clusters = [[True, False, False, False], [True, False]]
    assert _boot(clusters)["point"] == pytest.approx(2 / 6)


def test_perfectly_clustered_outcome_is_wider_than_binomial():
    """All-or-nothing episodes: cluster resampling must not shrink to binomial.

    Twenty episodes of five edges each, ten fully missed and ten clean.  A
    binomial at p=0.5 with n=100 has std 0.05; resampling whole episodes must be
    materially wider, which is exactly the anti-conservatism the independent
    bound hides.
    """
    clusters = [[True] * 5 for _ in range(10)] + [[False] * 5 for _ in range(10)]
    boot = _boot(clusters, iters=6000)
    deff = bootstrap_design_effect(boot, n_edges=100)
    assert boot["point"] == pytest.approx(0.5)
    assert deff["variance_ratio"] > 4.0
    assert boot["percentile_upper"] > 0.6


def test_homogeneous_clusters_stay_close_to_binomial():
    """No clustering: the bootstrap spread should sit near the binomial one."""
    rng = random.Random(11)
    clusters = [[rng.random() < 0.2 for _ in range(8)] for _ in range(60)]
    boot = _boot(clusters, iters=6000)
    ratio = bootstrap_design_effect(boot, n_edges=480)["variance_ratio"]
    assert 0.6 < ratio < 1.8


def test_bootstrap_is_deterministic_for_a_fixed_seed():
    clusters = [[True, False], [False, False], [True, True], [False, True]]
    assert _boot(clusters, seed="s") == _boot(clusters, seed="s")
    assert _boot(clusters, seed="s") != _boot(clusters, seed="other")


def test_zero_miss_option_bounds_at_zero_and_needs_the_cp_bound():
    """With no observed miss the percentile bootstrap collapses to 0.

    It cannot exceed the observed range, so on its own it would certify zero
    risk from zero evidence.  This is why the reported delta takes the max
    against Clopper-Pearson rather than the bootstrap alone.
    """
    boot = _boot([[False] * 5 for _ in range(20)])
    assert boot["percentile_upper"] == 0.0
    assert boot["std"] == 0.0


def test_design_effect_is_reported_but_never_applied():
    boot = _boot([[True, False], [False, False], [True, True]])
    assert bootstrap_design_effect(boot, n_edges=6)["applied"] is False


def test_single_episode_is_rejected_rather_than_silently_resampled():
    with pytest.raises(ValueError, match="at least two episodes"):
        _boot([[True, False]])


def test_option_family_matches_the_stochastic_bundles_being_corrected():
    assert OPTIONS == ("J1", "J3", "J4", "cascade")


def _hop_report(**deltas):
    return {"options": {k: {"delta_reported": v} for k, v in deltas.items()}}


def test_feasibility_is_length_times_delta_against_the_budget():
    out = feasibility(_hop_report(cheap=0.09, strong=0.23), eps_prop=0.2)
    cheap = out["per_option"]["cheap"]
    assert cheap["feasible_2hop"] is True
    assert cheap["max_uniform_path_length"] == 2
    assert cheap["headroom_2hop"] == pytest.approx(0.02)
    strong = out["per_option"]["strong"]
    # 0.23 alone already exceeds the whole budget: not even one edge fits.
    assert strong["feasible_1hop"] is False
    assert strong["max_uniform_path_length"] == 0


def test_feasibility_records_the_budget_it_was_given():
    assert feasibility(_hop_report(a=0.05), eps_prop=0.2)["eps_prop"] == 0.2


def test_misses_to_flip_counts_extra_misses_until_infeasible():
    """Thin margins must show up as a small number of edges."""
    clusters = [[True] * 5 + [False] * 45 for _ in range(2)]  # 10/100
    flip = misses_to_flip(
        clusters, n_edges=100, alpha=0.0125, budget=0.2, path_length=1
    )
    assert flip is not None and flip > 0
    tight = misses_to_flip(
        clusters, n_edges=100, alpha=0.0125, budget=0.2, path_length=2
    )
    # Doubling the path halves the effective budget, so it flips sooner.
    assert tight is None or tight < flip


def test_misses_to_flip_is_none_when_already_infeasible():
    clusters = [[True] * 40 + [False] * 10 for _ in range(2)]
    assert (
        misses_to_flip(clusters, n_edges=100, alpha=0.0125, budget=0.2, path_length=2)
        is None
    )


def test_replicated_bootstrap_takes_the_upper_envelope_and_records_the_band():
    rng = random.Random(3)
    clusters = [[rng.random() < 0.3 for _ in range(6)] for _ in range(40)]
    rep = _rep(clusters)
    band = rep["mc_noise"]
    assert rep["percentile_upper"] == max(band["per_replicate_upper"])
    assert band["spread"] == band["max_upper"] - band["min_upper"]
    assert len(band["per_replicate_upper"]) == 4


def test_replicates_below_two_are_rejected_so_noise_stays_measurable():
    with pytest.raises(ValueError, match="at least two replicates"):
        _rep([[True, False], [False, False]], replicates=1)


def test_compare_bounds_calls_a_gap_inside_the_noise_band_unresolved():
    boot = {"percentile_upper": 0.1010, "mc_noise": {"spread": 0.0020}}
    verdict = compare_bounds(0.1000, boot)
    assert verdict["gap_exceeds_mc_noise"] is False
    assert verdict["separation"] == "indistinguishable at this MC noise"
    # max still applies -- the band governs what may be claimed, not the choice.
    assert verdict["delta_reported"] == pytest.approx(0.1010)


def test_compare_bounds_reports_a_gap_beyond_the_band_as_material():
    boot = {"percentile_upper": 0.1300, "mc_noise": {"spread": 0.0015}}
    verdict = compare_bounds(0.1163, boot)
    assert verdict["gap_exceeds_mc_noise"] is True
    assert verdict["separation"] == "bootstrap materially larger"
    assert verdict["reported_source"] == "cluster-bootstrap"


def test_compare_bounds_keeps_the_cp_bound_when_it_is_the_larger_one():
    boot = {"percentile_upper": 0.0500, "mc_noise": {"spread": 0.0010}}
    verdict = compare_bounds(0.0891, boot)
    assert verdict["delta_reported"] == pytest.approx(0.0891)
    assert verdict["reported_source"] == "independent-cp"
    assert verdict["separation"] == "independent-cp larger"
