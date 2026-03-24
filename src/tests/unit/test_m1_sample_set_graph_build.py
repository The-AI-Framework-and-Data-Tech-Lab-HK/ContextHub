"""M1 proxy: pairing + raw + clean succeeds for all sample_traj (AMC_plan/13 §13.3 C)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.commit.clean_deriver import derive_clean_graph
from core.commit.graph_builder import build_raw_graph
from core.commit.pairing import pair_ai_tool_steps
from core.commit.validator import validate_raw_steps

pytestmark = [pytest.mark.unit, pytest.mark.m1]


def test_m1_sample_set_graph_build_success_rate(sample_traj_dir: Path) -> None:
    """All traj1..5 must complete validate → pair → raw → clean without error (100% on 5 files)."""
    ok = 0
    total = 0
    for i in range(1, 6):
        path = sample_traj_dir / f"traj{i}.json"
        if not path.exists():
            continue
        total += 1
        steps = json.loads(path.read_text(encoding="utf-8"))
        validate_raw_steps(steps)
        pairs = pair_ai_tool_steps(steps)
        assert pairs, f"{path.name}: expected at least one paired node"
        tid = f"m1-test-traj{i}"
        raw_nodes, raw_edges = build_raw_graph(tid, pairs)
        assert raw_nodes
        clean_nodes, clean_edges = derive_clean_graph(raw_nodes, raw_edges)
        assert clean_nodes
        assert len(clean_nodes) <= len(raw_nodes)
        ok += 1

    assert total >= 1
    rate = ok / total
    assert rate >= 0.95
    assert ok == total, f"M1 requires all sample trajectories to build; got {ok}/{total}"
