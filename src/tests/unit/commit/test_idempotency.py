"""U-09: idempotency key stability (AMC_plan/13)."""

from __future__ import annotations

import pytest

from core.commit.idempotency import commit_idempotency_key, trajectory_content_hash

pytestmark = pytest.mark.unit


def test_u09_same_payload_same_hash() -> None:
    traj = [{"Step": 1, "meta": {"role": "AIMessage"}, "Action": "x"}]
    h1 = trajectory_content_hash(traj)
    h2 = trajectory_content_hash(traj)
    assert h1 == h2


def test_u09_idempotency_key_stable() -> None:
    traj = [{"Step": 1, "meta": {"role": "AIMessage"}, "Action": "x"}]
    k1 = commit_idempotency_key("t1", "task-a", traj)
    k2 = commit_idempotency_key("t1", "task-a", traj)
    assert k1 == k2


def test_u09_different_task_different_key() -> None:
    traj = [{"Step": 1, "meta": {"role": "AIMessage"}}]
    assert commit_idempotency_key("t1", "a", traj) != commit_idempotency_key("t1", "b", traj)
