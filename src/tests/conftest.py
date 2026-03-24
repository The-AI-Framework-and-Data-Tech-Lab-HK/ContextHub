"""Shared fixtures for AMC tests (AMC_plan/13)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

# Repo root = parent of src/
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_TRAJ_DIR = PROJECT_ROOT / "sample_traj"


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def sample_traj_dir() -> Path:
    return SAMPLE_TRAJ_DIR


@pytest.fixture(params=[f"traj{i}.json" for i in range(1, 6)])
def sample_traj_file(request: pytest.FixtureRequest) -> Path:
    name: str = request.param
    p = SAMPLE_TRAJ_DIR / name
    if not p.exists():
        pytest.skip(f"missing fixture file {p}")
    return p


@pytest.fixture
def sample_traj_steps(sample_traj_file: Path) -> list[dict[str, Any]]:
    data = _load_json(sample_traj_file)
    assert isinstance(data, list)
    return data
