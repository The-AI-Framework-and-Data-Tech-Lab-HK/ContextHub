"""U-01: JSON load, Step monotonicity, meta.role (AMC_plan/13)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.commit.validator import TrajectoryValidationError, validate_raw_steps

pytestmark = pytest.mark.unit


def test_sample_traj_non_empty(sample_traj_steps: list[dict[str, Any]]) -> None:
    assert len(sample_traj_steps) > 0


def test_sample_traj_step_strictly_increasing(sample_traj_steps: list[dict[str, Any]]) -> None:
    steps = [int(s["Step"]) for s in sample_traj_steps]
    assert steps == sorted(steps)
    assert len(steps) == len(set(steps))


def test_sample_traj_roles_valid(sample_traj_steps: list[dict[str, Any]]) -> None:
    for s in sample_traj_steps:
        role = (s.get("meta") or {}).get("role")
        assert role in ("AIMessage", "ToolMessage")


def test_validate_raw_steps_accepts_all_sample_trajs(sample_traj_dir: Path) -> None:
    for i in range(1, 6):
        path = sample_traj_dir / f"traj{i}.json"
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            steps = json.load(f)
        validate_raw_steps(steps)


def test_validate_rejects_non_monotonic_step() -> None:
    bad = [
        {"Step": 1, "meta": {"role": "AIMessage"}},
        {"Step": 1, "meta": {"role": "ToolMessage"}},
    ]
    with pytest.raises(TrajectoryValidationError, match="strictly increasing"):
        validate_raw_steps(bad)
