"""Offline re-scoring of the frozen `Abs` runs under MEME's criterion.

All arithmetic that decides a reported number is tested here without an API key.
The tests against the real frozen runs are the ones that matter for the writeup:
they pin the denominators (90 / 29) and the stratification the report leans on.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from integrations.memebench.abs_official_rejudge import (
    JudgeCall,
    RejudgeError,
    build_case_rows,
    build_calls,
    build_plan,
    build_report,
    existing_verdicts,
    load_artifacts,
    no_signal_episodes,
    notices_requested,
    read_checkpoint,
    run_identity,
    strata,
    summarize,
    usage_attribution_audit,
    usd_cost,
    verdicts_from_rows,
)
from integrations.memebench.meme_official_judge import STAGES, parse_verdict
from integrations.memebench.run_abs_official_rejudge import (
    DEFAULT_PRICE_TABLE as _PRICE_TABLE,
    EXPECTED_CALLS,
    EXPECTED_EPISODES,
    INPUT_RUNS,
    parse_run_spec,
    preflight as runner_preflight,
    resolve_inputs,
)

_RUNS = Path(__file__).resolve().parents[1] / "integrations" / "memebench" / "runs"
_HOP1 = _RUNS / "formal_abs_hop1_20260902_final"
_HOP2 = _RUNS / "formal_abs_hop2_20260902_final"
_needs_runs = pytest.mark.skipif(
    not (_HOP1.exists() and _HOP2.exists()), reason="frozen Abs runs not present"
)


def _artifact(
    episode_id="pl_001",
    hop=1,
    *,
    answers=("old value", "old value", "Uncertain — previously 'old value'"),
    notices=(1,),
    three_part=(True, False, True),
    with_stale_notices=True,
):
    before, off, on = answers
    gold_after = "Uncertain — previously 'old value', but health_condition changed"
    return {
        "episode_id": episode_id,
        "evaluation_hop": hop,
        "task_type": "Abs",
        "with_stale_notices": with_stale_notices,
        "answers": {
            "before": {"raw_answer": before, "retrieval": {"stale_notices": []}},
            "off": {"raw_answer": off, "retrieval": {"stale_notices": []}},
            "on": {"raw_answer": on, "retrieval": {"stale_notices": list(notices)}},
        },
        "retrieval_evidence_contract": {
            "old_reference": "old value",
            "replacement_reference": gold_after,
        },
        "judge": {
            "calls": [
                {"stage": "before", "question": "Q?", "gold": "old value", "answer": before},
                {"stage": "off", "question": "Q?", "gold": gold_after, "answer": off},
                {"stage": "on", "question": "Q?", "gold": gold_after, "answer": on},
            ],
            "abs_scoring": {
                "stages": {
                    stage: {"correct": ok}
                    for stage, ok in zip(STAGES, three_part)
                }
            },
        },
    }


def _ok(correct: bool):
    return parse_verdict(
        '{"correct": %s, "reason": "r"}' % ("true" if correct else "false")
    )


# --- building the call plan ----------------------------------------------------


def test_build_calls_reuses_the_recorded_question_and_gold():
    """Only the prompt changes; the graded inputs are the run's own."""
    calls = {c.stage: c for c in build_calls(_artifact(), run="hop1")}
    assert set(calls) == set(STAGES)
    assert calls["on"].gold.startswith("Uncertain —")
    assert calls["before"].gold == "old value"
    assert calls["on"].question == "Q?"
    # Figure 18 for before, Figure 24 for the after arms.
    assert "semantically correct" in calls["before"].prompt
    assert "UNCERTAINTY" in calls["on"].prompt
    assert "UNCERTAINTY" in calls["off"].prompt


def test_build_calls_rejects_an_answer_that_differs_from_the_run():
    """A rejudge must never grade a different string than the run recorded."""
    artifact = _artifact()
    artifact["judge"]["calls"][2]["answer"] = "something else"
    with pytest.raises(RejudgeError, match="differs from raw_answer"):
        build_calls(artifact, run="hop1")


def test_build_calls_rejects_a_partial_case():
    """Trivial-pass needs the before stage, so a missing stage is fatal."""
    artifact = _artifact()
    artifact["judge"]["calls"] = artifact["judge"]["calls"][:2]
    with pytest.raises(RejudgeError, match="judge rows missing"):
        build_calls(artifact, run="hop1")


def test_build_calls_grades_an_empty_answer_rather_than_skipping_it():
    """An empty completion is a result, not missing data (one hop1 case is)."""
    calls = {c.stage: c for c in build_calls(_artifact(answers=("", "", "")), run="hop1")}
    assert calls["before"].answer == ""
    assert "AGENT: \n" in calls["before"].prompt


def test_call_key_is_unique_per_run_episode_stage():
    call = JudgeCall(
        run="hop1", episode_id="pl_001", hop=1, stage="on",
        question="q", gold="g", answer="a", prompt="p",
    )
    assert call.key == "hop1|pl_001|on"


# --- stratification ------------------------------------------------------------


def test_no_signal_is_derived_from_the_notices_not_hardcoded():
    """An ON arm with no stale notice cannot differ from OFF, so it is split out;
    deriving it means a rerun that fixes notices reclassifies automatically."""
    quiet = _artifact("sw_006", notices=())
    loud = _artifact("pl_001", notices=(1, 2))
    assert no_signal_episodes([quiet, loud]) == {"sw_006"}


def test_strata_are_subsets_of_all_and_partition_where_claimed():
    artifacts = [
        _artifact("pl_001", notices=(1,)),
        _artifact("sw_006", notices=()),
        _artifact("sw_010", notices=(1,)),
    ]
    got = strata(artifacts, hop=1)
    assert got["all"] == {"pl_001", "sw_006", "sw_010"}
    for name, ids in got.items():
        assert ids <= got["all"], name
    assert got["no_signal_on"] | got["signal_on"] == got["all"]
    assert not (got["no_signal_on"] & got["signal_on"])
    assert got["domain_pl"] | got["domain_sw"] == got["all"]


def test_hop2_splits_missing_edge_from_root_reachable():
    artifacts = [_artifact("sw_001", hop=2), _artifact("sw_007", hop=2)]
    got = strata(artifacts, hop=2)
    assert got["missing_edge"] == {"sw_001"}          # in HOP2_MISSING_EDGE
    assert got["root_reachable"] == {"sw_007"}
    assert "no_signal_on" not in got                  # hop1-only stratum


# --- aggregation ---------------------------------------------------------------


def _rows(*specs):
    """specs: (episode_id, official(before,off,on), three_part(before,off,on))"""
    artifacts, verdicts = [], {}
    for episode_id, official, three_part in specs:
        artifacts.append(_artifact(episode_id, three_part=three_part))
        for stage, ok in zip(STAGES, official):
            verdicts[f"r|{episode_id}|{stage}"] = _ok(ok)
    return artifacts, build_case_rows(artifacts, verdicts, run="r")


def test_summarize_reports_both_criteria_over_the_same_episodes():
    _, rows = _rows(
        ("a", (True, False, True), (True, False, True)),
        ("b", (True, False, True), (True, False, False)),
    )
    got = summarize(rows)
    assert got["n"] == 2
    assert got["on"]["official_raw"] == 2
    assert got["on"]["three_part"] == 1
    assert got["on"]["official_raw_rate"] == 1.0
    assert got["on"]["three_part_rate"] == 0.5


def test_trivial_pass_gate_lowers_the_official_number_when_before_failed():
    _, rows = _rows(
        ("a", (False, False, True), (False, False, True)),   # abstained, before wrong
        ("b", (True, False, True), (True, False, True)),
    )
    got = summarize(rows)
    assert got["on"]["official_raw"] == 2
    assert got["on"]["official_trivial_pass"] == 1
    assert got["before_official_ok"] == 1


def test_summarize_lists_disagreeing_episodes_rather_than_picking_a_winner():
    _, rows = _rows(
        ("a", (True, False, True), (True, False, False)),    # official yes, ours no
        ("b", (True, False, False), (True, False, True)),    # ours yes, official no
        ("c", (True, False, True), (True, False, True)),     # agree
    )
    got = summarize(rows)
    assert got["on"]["disagreements"] == ["a", "b"]
    assert got["on"]["n_disagreements"] == 2


def test_summarize_restricted_to_a_stratum_changes_the_denominator():
    _, rows = _rows(
        ("a", (True, False, True), (True, False, True)),
        ("b", (True, False, False), (True, False, False)),
    )
    assert summarize(rows)["n"] == 2
    only_a = summarize(rows, ["a"])
    assert only_a["n"] == 1
    assert only_a["on"]["official_raw"] == 1
    assert only_a["episode_ids"] == ["a"]


def test_summarize_of_an_empty_stratum_reports_n_zero_without_dividing():
    got = summarize([], [])
    assert got["n"] == 0
    assert "on" not in got


def test_parse_failures_are_counted_and_never_score_as_correct():
    artifacts = [_artifact("a")]
    verdicts = {f"r|a|{s}": _ok(True) for s in STAGES}
    verdicts["r|a|on"] = parse_verdict("CORRECT")   # our old contract, not MEME's
    rows = build_case_rows(artifacts, verdicts, run="r")
    got = summarize(rows)
    assert got["parse_failures"] == 1
    assert got["on"]["official_raw"] == 0


def test_build_case_rows_refuses_a_missing_verdict():
    artifacts = [_artifact("a")]
    verdicts = {"r|a|before": _ok(True), "r|a|off": _ok(True)}
    with pytest.raises(RejudgeError, match="missing official verdict"):
        build_case_rows(artifacts, verdicts, run="r")


def test_existing_verdicts_read_the_three_part_criterion_off_the_artifact():
    assert existing_verdicts(_artifact(three_part=(True, False, True))) == {
        "before": True, "off": False, "on": True,
    }


# --- checkpoint ----------------------------------------------------------------


def test_checkpoint_skips_truncated_and_incomplete_lines(tmp_path):
    path = tmp_path / "checkpoint.jsonl"
    path.write_text(
        json.dumps({"key": "r|a|on", "raw_output": '{"correct": true, "reason": "x"}'})
        + "\n"
        + json.dumps({"key": "r|a|off"})           # no raw_output: not completed
        + "\n"
        + '{"key": "r|a|before", "raw_out'         # hard-kill tail
        + "\n",
        encoding="utf-8",
    )
    done = read_checkpoint(path)
    assert set(done) == {"r|a|on"}
    assert verdicts_from_rows(done)["r|a|on"].correct is True


def test_checkpoint_absent_is_an_empty_resume(tmp_path):
    assert read_checkpoint(tmp_path / "nope.jsonl") == {}


def test_verdicts_are_reparsed_from_raw_output_not_trusted_from_disk():
    """A checkpoint's stored verdict field could be stale; the raw reply rules."""
    rows = {"k": {"key": "k", "raw_output": '{"correct": false, "reason": "r"}',
                  "correct": True}}
    assert verdicts_from_rows(rows)["k"].correct is False


def test_usage_attribution_audit_passes_when_deltas_sum_to_client_totals():
    rows = [
        {"usage": {"prompt_tokens": 200, "completion_tokens": 30, "calls": 1}},
        {"usage": {"prompt_tokens": 210, "completion_tokens": 25, "calls": 1}},
    ]
    totals = [{"prompt_tokens": 410, "completion_tokens": 55, "calls": 2}]
    assert usage_attribution_audit(rows, totals)["ok"] is True


def test_usage_attribution_audit_catches_shared_client_double_counting():
    """The real defect: concurrent calls on ONE CountingChatClient each read a
    before/after delta that absorbed the other's tokens, so the per-call sum
    overshoots what the client actually spent (measured 4x on 2026-09-02)."""
    rows = [
        {"usage": {"prompt_tokens": 800, "completion_tokens": 120, "calls": 1}},
        {"usage": {"prompt_tokens": 810, "completion_tokens": 118, "calls": 1}},
    ]
    totals = [{"prompt_tokens": 410, "completion_tokens": 55, "calls": 2}]
    audit = usage_attribution_audit(rows, totals)
    assert audit["ok"] is False
    assert audit["per_call_sum"]["prompt_tokens"] > audit["client_totals"]["prompt_tokens"]


def test_usage_attribution_audit_sums_across_one_client_per_slot():
    """The fix's shape: several clients, each serving one call at a time."""
    rows = [
        {"usage": {"prompt_tokens": 100, "completion_tokens": 10, "calls": 1}},
        {"usage": {"prompt_tokens": 100, "completion_tokens": 10, "calls": 1}},
        {"usage": {"prompt_tokens": 100, "completion_tokens": 10, "calls": 1}},
    ]
    totals = [
        {"prompt_tokens": 200, "completion_tokens": 20, "calls": 2},
        {"prompt_tokens": 100, "completion_tokens": 10, "calls": 1},
    ]
    audit = usage_attribution_audit(rows, totals)
    assert audit["ok"] is True
    assert audit["n_clients"] == 2
    assert audit["n_rows_audited"] == 3


def test_usd_cost_prices_at_the_given_rate():
    assert usd_cost(
        1_000_000, 1_000_000, {"input_per_million": 2.5, "output_per_million": 10.0}
    ) == pytest.approx(12.5)


# --- against the real frozen runs ----------------------------------------------


# --- no-notices arm: strata guard and CLI input resolution ---------------------


def test_notices_requested_reads_the_recorded_flag():
    assert notices_requested([_artifact(with_stale_notices=True)]) is True
    assert notices_requested([_artifact(with_stale_notices=False)]) is False


def test_notices_requested_refuses_to_guess_a_missing_flag():
    """The defaulted-field defect class: a guess here silently redefines a stratum."""
    artifact = _artifact()
    del artifact["with_stale_notices"]
    with pytest.raises(RejudgeError, match="with_stale_notices"):
        notices_requested([artifact])


def test_notices_requested_rejects_a_run_that_mixes_both():
    artifacts = [
        _artifact("pl_001", with_stale_notices=True),
        _artifact("pl_002", with_stale_notices=False),
    ]
    with pytest.raises(RejudgeError, match="mixes"):
        notices_requested(artifacts)


def test_no_notices_run_omits_the_notice_derived_strata_rather_than_degenerating():
    """Without this guard the report would claim propagation produced no signal.

    A --no-stale-notices run has zero notices on every episode by construction, so
    the derived split would read no_signal_on = all, signal_on = empty -- which
    describes propagation having failed everywhere, when it in fact ran normally
    and was merely not explained to the model.
    """
    artifacts = [
        _artifact("pl_001", notices=(), with_stale_notices=False),
        _artifact("sw_010", notices=(), with_stale_notices=False),
    ]
    names = strata(artifacts, hop=1)
    assert "no_signal_on" not in names
    assert "signal_on" not in names
    # The strata that do not depend on notices are unaffected.
    assert len(names["all"]) == 2
    assert names["domain_pl"] == {"pl_001"}
    assert names["domain_sw"] == {"sw_010"}


def test_notices_run_still_emits_the_split():
    artifacts = [_artifact("pl_001", notices=(1,)), _artifact("sw_006", notices=())]
    names = strata(artifacts, hop=1)
    assert names["no_signal_on"] == {"sw_006"}
    assert names["signal_on"] == {"pl_001"}


def test_report_records_the_flag_and_explains_an_omitted_stratum():
    artifacts = [_artifact("pl_001", notices=(), with_stale_notices=False)]
    verdicts = {f"x|pl_001|{s}": _ok(True) for s in STAGES}
    rows = build_case_rows(artifacts, verdicts, run="x")
    block = build_report({"x": (artifacts, rows)})["runs"]["x"]
    assert block["with_stale_notices"] is False
    assert set(block["omitted_strata"]) == {"no_signal_on", "signal_on"}


def test_report_marks_a_notices_run_without_omissions():
    artifacts = [_artifact("pl_001", notices=(1,))]
    verdicts = {f"x|pl_001|{s}": _ok(True) for s in STAGES}
    rows = build_case_rows(artifacts, verdicts, run="x")
    block = build_report({"x": (artifacts, rows)})["runs"]["x"]
    assert block["with_stale_notices"] is True
    assert "omitted_strata" not in block


def test_parse_run_spec_resolves_pairs_and_rejects_malformed_input():
    runs = parse_run_spec(["hop1=runs/a", "hop1_nonotices=runs/b"])
    assert set(runs) == {"hop1", "hop1_nonotices"}
    assert all(p.is_absolute() for p in runs.values())
    assert parse_run_spec(None) is None
    with pytest.raises(RejudgeError, match="name=path"):
        parse_run_spec(["runs/a"])
    with pytest.raises(RejudgeError, match="unique"):
        parse_run_spec(["x=runs/a", "x=runs/b"])


def test_no_runs_flag_keeps_the_published_defaults_untouched():
    """The published command must keep stamping the same identity."""
    args = argparse.Namespace(runs=None, expect_episodes=None, expected_calls=None)
    runs, episodes, calls = resolve_inputs(args)
    assert runs == dict(INPUT_RUNS)
    assert episodes == dict(EXPECTED_EPISODES)
    assert calls == EXPECTED_CALLS


def test_runs_flag_derives_expected_calls_as_three_per_episode():
    args = argparse.Namespace(
        runs=["hop1_nonotices=runs/b"],
        expect_episodes=["hop1_nonotices=90"],
        expected_calls=None,
    )
    runs, episodes, calls = resolve_inputs(args)
    assert set(runs) == {"hop1_nonotices"}
    assert episodes == {"hop1_nonotices": 90}
    assert calls == 270


def test_runs_flag_requires_the_episode_counts():
    """Dropping the count check would let a partial run redefine every denominator."""
    args = argparse.Namespace(runs=["a=runs/b"], expect_episodes=None, expected_calls=None)
    with pytest.raises(RejudgeError, match="expect-episodes"):
        resolve_inputs(args)


def test_runs_and_expected_episodes_must_name_the_same_runs():
    args = argparse.Namespace(
        runs=["a=runs/b"], expect_episodes=["c=90"], expected_calls=None
    )
    with pytest.raises(RejudgeError, match="exactly"):
        resolve_inputs(args)


@_needs_runs
def test_preflight_enforces_a_wrong_episode_count_for_a_custom_run():
    """The guard that a custom run directory is complete actually fires."""
    with pytest.raises(RejudgeError, match="expected 89 artifacts"):
        runner_preflight(
            {"hop1": _HOP1},
            _PRICE_TABLE,
            None,
            expected_episodes={"hop1": 89},
            expected_calls=267,
        )


@_needs_runs
def test_preflight_records_that_the_frozen_runs_had_notices_on():
    checks = runner_preflight({"hop1": _HOP1}, _PRICE_TABLE, None,
                              expected_episodes={"hop1": 90}, expected_calls=270)["checks"]
    assert checks["with_stale_notices"] == {"hop1": True}


@_needs_runs
def test_the_real_plan_is_357_calls_over_119_episodes():
    plan = build_plan({"hop1": _HOP1, "hop2": _HOP2})
    assert len(plan) == 357
    assert len({c.key for c in plan}) == 357
    # 119 (run, episode) pairs, but only 94 distinct episode ids: 25 episodes are
    # evaluated at BOTH hops, which is why the call key is namespaced by run.
    # (It also means hop1 and hop2 are not independent samples -- they are
    # reported separately, never pooled.)
    assert len({(c.run, c.episode_id) for c in plan}) == 119
    assert len({c.episode_id for c in plan}) == 94
    for stage in STAGES:
        assert sum(1 for c in plan if c.stage == stage) == 119


@_needs_runs
def test_the_real_strata_match_the_denominators_the_report_uses():
    hop1 = strata(load_artifacts(_HOP1), hop=1)
    assert len(hop1["all"]) == 90
    # Derived, and it lands exactly on the three episodes identified by hand.
    assert hop1["no_signal_on"] == {"sw_006", "sw_025", "sw_045"}
    assert len(hop1["signal_on"]) == 87

    hop2 = strata(load_artifacts(_HOP2), hop=2)
    assert len(hop2["all"]) == 29
    assert len(hop2["missing_edge"]) == 8
    assert len(hop2["root_reachable"]) == 21


@_needs_runs
def test_run_identity_stamps_paper_pinned_prompt_hashes():
    runs = {"hop1": _HOP1, "hop2": _HOP2}
    identity = run_identity(
        judge_model="gpt-4o", provider="openlux", runs=runs, plan=build_plan(runs)
    )
    assert identity["judge_temperature"] == 0.0
    assert identity["n_calls"] == 357
    assert identity["artifacts_mutated"] is False
    assert identity["generation_rerun"] is False
    # Same inputs must give the same plan hash, or resume/compare is meaningless.
    again = run_identity(
        judge_model="gpt-4o", provider="openlux", runs=runs, plan=build_plan(runs)
    )
    assert identity["plan_sha256"] == again["plan_sha256"]


@_needs_runs
def test_build_report_covers_every_episode_per_run():
    """Report shape, with the official verdicts stubbed: no API, no scores."""
    per_run = {}
    for name, run_dir, hop in (("hop1", _HOP1, 1), ("hop2", _HOP2, 2)):
        artifacts = load_artifacts(run_dir)
        verdicts = {
            f"{name}|{a['episode_id']}|{s}": _ok(True)
            for a in artifacts
            for s in STAGES
        }
        per_run[name] = (artifacts, build_case_rows(artifacts, verdicts, run=name))
    report = build_report(per_run)
    assert report["runs"]["hop1"]["n_episodes"] == 90
    assert report["runs"]["hop2"]["n_episodes"] == 29
    assert report["runs"]["hop2"]["hop"] == 2
    assert report["runs"]["hop1"]["strata"]["all"]["n"] == 90
    assert report["runs"]["hop2"]["strata"]["root_reachable"]["n"] == 21
