"""Pairing two `Abs` runs: the guards that stop a mis-read comparison.

Zero API. The tests that matter are the ones pinning *which run's labels* the
strata come from, and that the two runs cannot be swapped silently.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from integrations.memebench.abs_cross_run_compare import (
    CompareError,
    compare,
    load_run,
    noise_floor,
    notices_flag,
)


def _write(run_dir: Path, episodes) -> Path:
    """Materialise a minimal run directory the loader accepts."""
    for artifact in episodes:
        case = run_dir / "artifacts" / "full-run" / f"{artifact['episode_id']}-Abs-x"
        case.mkdir(parents=True, exist_ok=True)
        (case / "artifact.json").write_text(json.dumps(artifact), encoding="utf-8")
    return run_dir


def _episode(
    episode_id="pl_001",
    *,
    with_notices=True,
    n_notices=2,
    off_answer="old value",
    on_answer="Uncertain — previously 'old value', but health changed",
    off_three=(False, True, False),
    on_three=(True, True, True),
):
    def stage(triple):
        abstained, cited, named = triple
        return {
            "abstained": abstained,
            "cited_prev": cited,
            "named_upstream": named,
            "correct": all(triple),
        }

    return {
        "episode_id": episode_id,
        "evaluation_hop": 1,
        "task_type": "Abs",
        "with_stale_notices": with_notices,
        "answers": {
            "before": {"raw_answer": "old value", "retrieval": {"stale_notices": []}},
            "off": {"raw_answer": off_answer, "retrieval": {"stale_notices": []}},
            "on": {
                "raw_answer": on_answer,
                "retrieval": {"stale_notices": [{"i": i} for i in range(n_notices)]},
            },
        },
        "judge": {
            "abs_scoring": {
                "stages": {
                    "before": stage((False, True, False)),
                    "off": stage(off_three),
                    "on": stage(on_three),
                }
            }
        },
    }


def test_notices_flag_refuses_to_guess_a_missing_field():
    artifact = _episode()
    del artifact["with_stale_notices"]
    with pytest.raises(CompareError, match="with_stale_notices"):
        notices_flag({"pl_001": artifact}, "x")


def test_compare_refuses_when_the_runs_are_passed_in_the_wrong_order(tmp_path):
    """Swapping them would silently invert every reported difference."""
    a = _write(tmp_path / "a", [_episode(with_notices=True)])
    b = _write(tmp_path / "b", [_episode(with_notices=False, n_notices=0)])
    with pytest.raises(CompareError, match="WITHOUT notices"):
        compare(b, a)


def test_compare_pairs_shared_episodes_and_lists_the_rest(tmp_path):
    a = _write(
        tmp_path / "a",
        [_episode("pl_001"), _episode("pl_002"), _episode("pl_003")],
    )
    b = _write(
        tmp_path / "b",
        [
            _episode("pl_001", with_notices=False, n_notices=0),
            _episode("pl_002", with_notices=False, n_notices=0),
            _episode("sw_009", with_notices=False, n_notices=0),
        ],
    )
    report = compare(a, b)
    assert report["pairing"]["shared"] == 2
    assert report["pairing"]["only_in_A"] == ["pl_003"]
    assert report["pairing"]["only_in_B"] == ["sw_009"]


def test_strata_use_the_notices_run_labels_not_the_no_notices_run(tmp_path):
    """The whole point: B has zero notices everywhere by construction.

    Deriving signal_on from B would put every episode in no_signal_on and report
    that propagation produced no signal anywhere, which is false -- propagation
    ran, it just was not explained to the model.
    """
    a = _write(
        tmp_path / "a",
        [_episode("pl_001", n_notices=3), _episode("sw_006", n_notices=0)],
    )
    b = _write(
        tmp_path / "b",
        [
            _episode("pl_001", with_notices=False, n_notices=0),
            _episode("sw_006", with_notices=False, n_notices=0),
        ],
    )
    strata = compare(a, b)["strata"]
    assert strata["signal_on"]["episode_ids"] == ["pl_001"]
    assert strata["no_signal_on"]["episode_ids"] == ["sw_006"]


def test_noise_floor_counts_verdict_flips_not_just_reworded_text(tmp_path):
    """A reworded answer that scores the same moves no reported number."""
    a_run = {
        "pl_001": _episode("pl_001", off_answer="tutoring", off_three=(False, True, False)),
        "pl_002": _episode("pl_002", off_answer="gym", off_three=(False, True, False)),
    }
    b_run = {
        # same verdict, different string -> text differs, no flip
        "pl_001": _episode(
            "pl_001", with_notices=False, off_answer="tutoring session",
            off_three=(False, True, False),
        ),
        # different verdict -> a real flip
        "pl_002": _episode(
            "pl_002", with_notices=False, off_answer="gym", off_three=(True, True, True),
        ),
    }
    nf = noise_floor(a_run, b_run, ["pl_001", "pl_002"])
    assert nf["off"]["identical_text"] == 1
    assert nf["off"]["verdict_flipped"] == 1
    assert nf["off"]["verdict_flipped_episodes"] == ["pl_002"]
    assert nf["max_verdict_flips"] == 1


def test_load_run_rejects_an_empty_directory(tmp_path):
    (tmp_path / "artifacts" / "full-run").mkdir(parents=True)
    with pytest.raises(CompareError, match="no full-run artifacts"):
        load_run(tmp_path)


def test_arm_rates_are_computed_over_the_same_episode_set_for_both_runs(tmp_path):
    """Denominators must match, or the three differences are not comparable."""
    a = _write(tmp_path / "a", [_episode("pl_001"), _episode("pl_002")])
    b = _write(
        tmp_path / "b",
        [
            _episode("pl_001", with_notices=False, n_notices=0, on_three=(False, True, False)),
            _episode("pl_002", with_notices=False, n_notices=0, on_three=(False, True, False)),
        ],
    )
    block = compare(a, b)["strata"]["all"]
    assert block["A_on"]["n"] == block["B_on"]["n"] == block["B_off"]["n"] == 2
    assert block["A_on"]["all_three"] == 2
    assert block["B_on"]["all_three"] == 0
