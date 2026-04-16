from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.orchestrators.commit_orchestrator import PreparedCommitOutcome
from cli.commit_trajectory_batch import (
    _RuntimeBundle,
    _default_alfworld_paths,
    run_commit_batch,
)
from core.commit.validator import TrajectoryValidationError


def _write_traj(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "trajectory": [
                    {"Step": 1, "Action": "x", "Action_input": "x", "meta": {"role": "AIMessage"}},
                    {"Step": 2, "Action_result": "ok", "meta": {"role": "ToolMessage"}},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class _FakeResult:
    def __init__(self, trajectory_id: str, status: str = "accepted") -> None:
        self.trajectory_id = trajectory_id
        self.idempotency_key = f"idem-{trajectory_id}"
        self.status = status
        self.nodes = 3
        self.edges = 2
        self.warnings: list[str] = []
        self.payload = {
            "neo4j_summary": {"enabled": True, "ok": True},
            "vector_index_summary": {"enabled": True, "ok": True},
        }


def test_default_alfworld_paths_0001_to_0008(tmp_path: Path) -> None:
    for i in range(1, 9):
        _write_traj(tmp_path / f"traj_alfworld_{i:04d}.json")
    resolved = _default_alfworld_paths(tmp_path, start=1, end=8)
    assert len(resolved) == 8
    assert str(resolved[0]).endswith("traj_alfworld_0001.json")
    assert str(resolved[-1]).endswith("traj_alfworld_0008.json")


def test_run_commit_batch_default_inputs_success(monkeypatch, tmp_path: Path) -> None:
    for i in range(1, 9):
        _write_traj(tmp_path / f"traj_alfworld_{i:04d}.json")

    class FakeOrchestrator:
        def prepare_commits(self, commands, *, max_workers=1):  # noqa: ANN001
            out = []
            for idx, cmd in enumerate(commands):
                out.append(
                    PreparedCommitOutcome(
                        command=cmd,
                        result=_FakeResult(f"traj-{idx}"),
                        error=None,
                    )
                )
            return out

        def commit_prepared(self, command, result):  # noqa: ANN001
            return result

        def replay(self, trajectory_id: str):  # noqa: ARG002
            return {
                "base_path": "/tmp/dummy",
                "graph_pointer": {
                    "raw_graph_file": "/tmp/dummy/raw_graph.json",
                    "clean_graph_file": "/tmp/dummy/clean_graph.json",
                },
            }

    monkeypatch.setattr(
        "cli.commit_trajectory_batch._build_runtime",
        lambda **kwargs: _RuntimeBundle(
            orchestrator=FakeOrchestrator(),
            extractor_obj=SimpleNamespace(model="", api_key="", base_url=""),
            summarizer_obj=SimpleNamespace(model="", api_key="", base_url=""),
        ),
    )

    result = run_commit_batch(
        trajectory_files=[],
        alfworld_dir=str(tmp_path),
        range_start=1,
        range_end=8,
        account_id="acc",
        agent_id="agent",
        scope="agent",
        owner_space=None,
        session_prefix="s",
        task_prefix="t",
        labels={},
        fail_fast=False,
        llm_batch_size_hint=8,
        llm_max_items_per_batch=16,
        llm_token_usage_ratio=0.6,
        llm_max_context_tokens_fallback=24000,
        visualize_graph_png=False,
        disable_idempotency=False,
        config_path=None,
    )
    assert result["status"] == "accepted"
    assert result["summary"]["total"] == 8
    assert result["summary"]["accepted"] == 8
    assert result["summary"]["failed"] == 0
    assert result["summary"]["extraction_success"] == 8
    assert "resolved_inputs" not in result
    assert len(result["items"]) == 8
    assert all(item["extraction_success"] is True for item in result["items"])
    assert result["timing"]["total_seconds"] >= 0.0


def test_run_commit_batch_fail_fast_skips_remaining(monkeypatch, tmp_path: Path) -> None:
    p1 = tmp_path / "a.json"
    p2 = tmp_path / "b.json"
    _write_traj(p1)
    _write_traj(p2)

    class FakeOrchestrator:
        def __init__(self) -> None:
            self.calls = 0

        def commit(self, command):  # noqa: ANN001
            self.calls += 1
            if self.calls == 1:
                raise TrajectoryValidationError("bad trajectory")
            return _FakeResult("traj-ok")

        def replay(self, trajectory_id: str):  # noqa: ARG002
            return None

    monkeypatch.setattr(
        "cli.commit_trajectory_batch._build_runtime",
        lambda **kwargs: _RuntimeBundle(
            orchestrator=FakeOrchestrator(),
            extractor_obj=None,
            summarizer_obj=None,
        ),
    )

    result = run_commit_batch(
        trajectory_files=[str(p1), str(p2)],
        alfworld_dir=str(tmp_path),
        range_start=1,
        range_end=8,
        account_id="acc",
        agent_id="agent",
        scope="agent",
        owner_space=None,
        session_prefix="s",
        task_prefix="t",
        labels={},
        fail_fast=True,
        llm_batch_size_hint=8,
        llm_max_items_per_batch=16,
        llm_token_usage_ratio=0.6,
        llm_max_context_tokens_fallback=24000,
        visualize_graph_png=False,
        disable_idempotency=False,
        config_path=None,
    )
    assert result["status"] == "accepted_partial"
    assert result["summary"]["total"] == 2
    assert result["summary"]["failed"] == 1
    assert result["summary"]["skipped"] == 1
    assert result["summary"]["extraction_success"] == 0
    assert result["items"][0]["status"] == "failed"
    assert result["items"][0]["extraction_success"] is False
    assert result["items"][1]["status"] == "skipped"
    assert result["items"][1]["extraction_success"] is None


def test_run_commit_batch_full_mode_keeps_detailed_fields(monkeypatch, tmp_path: Path) -> None:
    p1 = tmp_path / "one.json"
    _write_traj(p1)

    class FakeOrchestrator:
        def prepare_commits(self, commands, *, max_workers=1):  # noqa: ANN001
            return [PreparedCommitOutcome(command=commands[0], result=_FakeResult("traj-one"), error=None)]

        def commit_prepared(self, command, result):  # noqa: ANN001
            return result

        def replay(self, trajectory_id: str):  # noqa: ARG002
            return {
                "base_path": "/tmp/dummy",
                "graph_pointer": {
                    "raw_graph_file": "/tmp/dummy/raw_graph.json",
                    "clean_graph_file": "/tmp/dummy/clean_graph.json",
                },
            }

    monkeypatch.setattr(
        "cli.commit_trajectory_batch._build_runtime",
        lambda **kwargs: _RuntimeBundle(
            orchestrator=FakeOrchestrator(),
            extractor_obj=SimpleNamespace(model="", api_key="", base_url=""),
            summarizer_obj=SimpleNamespace(model="", api_key="", base_url=""),
        ),
    )

    result = run_commit_batch(
        trajectory_files=[str(p1)],
        alfworld_dir=str(tmp_path),
        range_start=1,
        range_end=8,
        account_id="acc",
        agent_id="agent",
        scope="agent",
        owner_space=None,
        session_prefix="s",
        task_prefix="t",
        labels={},
        fail_fast=False,
        llm_batch_size_hint=8,
        llm_max_items_per_batch=16,
        llm_token_usage_ratio=0.6,
        llm_max_context_tokens_fallback=24000,
        visualize_graph_png=False,
        disable_idempotency=False,
        config_path=None,
        output_mode="full",
    )
    assert result["summary"]["total"] == 1
    assert "resolved_inputs" in result
    assert "vector_index_summary" in result["items"][0]
    assert "storage" in result["items"][0]

