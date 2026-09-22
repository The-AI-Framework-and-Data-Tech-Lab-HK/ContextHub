"""The hop2 stratification is the one thing block 8 must not get wrong.

A flat hop2 number mixes 21 episodes whose root is reachable with 8 whose chain
is real in the dataset but missing from the frozen v3 graph. The 8 are expected
to fail and are kept in the denominator deliberately (they are a P1 recall hole,
not a dataset defect), so collapsing them would read as a propagation failure
that did not happen.
"""

from __future__ import annotations

import json
from pathlib import Path

from integrations.memebench.abs_report import (
    HOP2_MISSING_EDGE,
    load_artifacts,
    summarize,
)


def _artifact(episode_id: str, *, hop: int, on_correct: bool) -> dict:
    return {
        "episode_id": episode_id,
        "task_type": "Abs",
        "evaluation_hop": hop,
        "judge": {
            "abs_scoring": {
                "stages": {
                    "before": {"correct": True, "criterion": "containment"},
                    "off": {
                        "abstained": False,
                        "cited_prev": True,
                        "named_upstream": False,
                        "correct": False,
                    },
                    "on": {
                        "abstained": True,
                        "cited_prev": True,
                        "named_upstream": on_correct,
                        "correct": on_correct,
                    },
                }
            }
        },
    }


def _write(run_dir: Path, artifacts: list[dict]) -> None:
    for artifact in artifacts:
        case = run_dir / "artifacts" / "full-run" / f"{artifact['episode_id']}-Abs-0123456789ab"
        case.mkdir(parents=True)
        (case / "artifact.json").write_text(json.dumps(artifact), encoding="utf-8")


def test_missing_edge_set_is_the_eight_named_in_the_plan():
    assert HOP2_MISSING_EDGE == {
        "sw_001",
        "sw_009",
        "sw_010",
        "sw_019",
        "sw_028",
        "sw_036",
        "sw_044",
        "sw_046",
    }
    assert all(episode_id.startswith("sw") for episode_id in HOP2_MISSING_EDGE)


def test_hop2_strata_partition_without_dropping_or_double_counting(tmp_path: Path):
    """The two strata must sum back to the full tier: keeping the 8 in the
    denominator is the whole point, so neither stratum may silently drop them."""
    reachable_ids = [f"sw_1{i:02d}" for i in range(21)]
    artifacts = [_artifact(e, hop=2, on_correct=True) for e in reachable_ids]
    artifacts += [_artifact(e, hop=2, on_correct=False) for e in sorted(HOP2_MISSING_EDGE)]
    _write(tmp_path, artifacts)

    loaded = load_artifacts(tmp_path)
    assert len(loaded) == 29

    reachable = [a for a in loaded if a["episode_id"] not in HOP2_MISSING_EDGE]
    missing = [a for a in loaded if a["episode_id"] in HOP2_MISSING_EDGE]
    assert len(reachable) == 21
    assert len(missing) == 8
    assert len(reachable) + len(missing) == len(loaded)

    # The flat number would read 21/29 = 72.4% and look like a partial
    # propagation failure; stratified it is 21/21 and 0/8.
    assert summarize(reachable, arm="on")["all_three"] == 21
    assert summarize(missing, arm="on")["all_three"] == 0
    assert summarize(loaded, arm="on")["all_three"] == 21


def test_summarize_counts_each_stage_independently(tmp_path: Path):
    """Stages 1-2 measure propagation; stage 3 is capped by the frozen graph
    having no entity labels. Reporting only the conjunction hides that."""
    artifacts = [_artifact(f"pl_0{i:02d}", hop=1, on_correct=(i < 4)) for i in range(10)]
    _write(tmp_path, artifacts)
    summary = summarize(load_artifacts(tmp_path), arm="on")
    assert summary["n"] == 10
    assert summary["abstained"] == 10
    assert summary["cited_prev"] == 10
    assert summary["named_upstream"] == 4
    assert summary["all_three"] == 4
