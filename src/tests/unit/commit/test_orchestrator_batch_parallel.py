"""Unit tests for Phase B batch commit orchestration."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.orchestrators.commit_orchestrator import CommitOrchestrator
from core.commit.service import CommitCommand, CommitResult


class _FakeCommitService:
    def __init__(self, *, sleep_s: float = 0.2) -> None:
        self.sleep_s = sleep_s

    def run(self, cmd: CommitCommand) -> CommitResult:
        time.sleep(self.sleep_s)
        if "bad" in cmd.task_id:
            raise ValueError(f"bad task: {cmd.task_id}")
        return CommitResult(
            trajectory_id=f"traj-{cmd.task_id}",
            idempotency_key=f"idem-{cmd.task_id}",
            status="accepted",
            nodes=1,
            edges=0,
            warnings=[],
            summary_l0="l0",
            summary_l1="l1",
            payload={},
        )


class _FakeRepo:
    def find_trajectory_id_by_idempotency_key(self, key: str) -> str | None:
        return None

    def save_bundle(self, **kwargs: Any) -> str:
        return str(Path("/tmp/amc-test"))

    def load_trajectory(self, trajectory_id: str) -> dict[str, Any] | None:
        return None


class _FakeAudit:
    def write(self, **kwargs: Any) -> None:
        return None


def _command(i: int, *, task_suffix: str = "") -> CommitCommand:
    return CommitCommand(
        agent_id="agent-test",
        account_id="account-test",
        scope="agent",
        owner_space="agent-test",
        session_id=f"session-{i}",
        task_id=f"task-{i}{task_suffix}",
        trajectory=[{"Step": 1, "meta": {"role": "AIMessage"}}],
        labels={},
        is_incremental=False,
    )


def test_prepare_commits_parallel_is_faster_than_serial() -> None:
    orchestrator = CommitOrchestrator(
        commit_service=_FakeCommitService(sleep_s=0.2),
        repo=_FakeRepo(),
        audit=_FakeAudit(),
        graph_store=None,
        vector_indexer=None,
        idempotency_enabled=False,
    )
    commands = [_command(i) for i in range(4)]

    t0 = time.perf_counter()
    serial = orchestrator.prepare_commits(commands, max_workers=1)
    serial_elapsed = time.perf_counter() - t0

    t1 = time.perf_counter()
    parallel = orchestrator.prepare_commits(commands, max_workers=4)
    parallel_elapsed = time.perf_counter() - t1

    assert all(x.error is None for x in serial)
    assert all(x.error is None for x in parallel)
    # Parallel outcomes must preserve original command order.
    assert [x.command.task_id for x in parallel] == [c.task_id for c in commands]
    # With four same-cost tasks, parallel should significantly reduce wall-clock time.
    assert parallel_elapsed < serial_elapsed * 0.7


def test_prepare_commits_collects_item_errors_without_crashing() -> None:
    orchestrator = CommitOrchestrator(
        commit_service=_FakeCommitService(sleep_s=0.05),
        repo=_FakeRepo(),
        audit=_FakeAudit(),
        graph_store=None,
        vector_indexer=None,
        idempotency_enabled=False,
    )
    commands = [_command(1), _command(2, task_suffix="-bad"), _command(3)]

    outcomes = orchestrator.prepare_commits(commands, max_workers=3)

    assert len(outcomes) == 3
    assert outcomes[0].error is None
    assert outcomes[1].error is not None
    assert isinstance(outcomes[1].error, ValueError)
    assert outcomes[2].error is None

