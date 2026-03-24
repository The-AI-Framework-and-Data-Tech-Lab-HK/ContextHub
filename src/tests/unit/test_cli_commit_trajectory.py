"""CLI unit test: commit trajectory and return storage locations."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from cli.commit_trajectory import run_commit

pytestmark = pytest.mark.unit


def test_cli_run_commit_returns_storage_paths(sample_traj_dir: Path, tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "\n".join(
            [
                "app:",
                "  env: dev",
                "api:",
                '  prefix: "/api/v1/amc"',
                "commit:",
                "  normalize:",
                "    max_action_result_chars: 12000",
                "storage:",
                "  content_store:",
                f'    localfs_root: "{(tmp_path / "content").as_posix()}"',
                "  event_log:",
                f'    jsonl_path: "{(tmp_path / "events" / "events.jsonl").as_posix()}"',
                "audit:",
                f'  file_path: "{(tmp_path / "audit" / "audit.jsonl").as_posix()}"',
            ]
        ),
        encoding="utf-8",
    )
    out = run_commit(
        trajectory_file=sample_traj_dir / "traj1.json",
        tenant_id="tenant-cli",
        agent_id="agent-cli",
        session_id="session-cli",
        task_id="task-cli-1",
        task_type="sql_analysis",
        trajectory_id=None,
        config_path=str(cfg),
    )
    assert out["status"] in ("accepted", "idempotent")
    storage = out["storage"]
    assert Path(storage["base_path"]).exists()
    assert Path(storage["l0_abstract_path"]).exists()
    assert Path(storage["l1_overview_path"]).exists()
    assert Path(storage["graph_pointer_path"]).exists()
    assert Path(storage["raw_graph_path"]).exists()
    assert Path(storage["clean_graph_path"]).exists()

    # Ensure graph pointer has readable JSON structure.
    gp = json.loads(Path(storage["graph_pointer_path"]).read_text(encoding="utf-8"))
    assert "raw_graph_file" in gp
    assert "clean_graph_file" in gp


def test_cli_visualization_off_by_default(sample_traj_dir: Path, tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "\n".join(
            [
                "storage:",
                "  content_store:",
                f'    localfs_root: "{(tmp_path / "content").as_posix()}"',
                "  event_log:",
                f'    jsonl_path: "{(tmp_path / "events" / "events.jsonl").as_posix()}"',
                "audit:",
                f'  file_path: "{(tmp_path / "audit" / "audit.jsonl").as_posix()}"',
            ]
        ),
        encoding="utf-8",
    )
    out = run_commit(
        trajectory_file=sample_traj_dir / "traj1.json",
        tenant_id="tenant-cli",
        agent_id="agent-cli",
        session_id="session-cli",
        task_id=f"task-cli-2-{uuid4().hex}",
        task_type="sql_analysis",
        trajectory_id=None,
        visualize_graph_png=False,
        config_path=str(cfg),
    )
    base = Path(out["storage"]["base_path"])
    assert not (base / "raw_graph.png").exists()
    assert not (base / "clean_graph.png").exists()


def test_cli_disable_idempotency_forces_update(sample_traj_dir: Path, tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "\n".join(
            [
                "commit:",
                "  idempotency:",
                "    enabled: true",
                "storage:",
                "  content_store:",
                f'    localfs_root: "{(tmp_path / "content").as_posix()}"',
                "  event_log:",
                f'    jsonl_path: "{(tmp_path / "events" / "events.jsonl").as_posix()}"',
                "audit:",
                f'  file_path: "{(tmp_path / "audit" / "audit.jsonl").as_posix()}"',
            ]
        ),
        encoding="utf-8",
    )
    common = dict(
        trajectory_file=sample_traj_dir / "traj1.json",
        tenant_id="tenant-cli",
        agent_id="agent-cli",
        session_id="session-cli",
        task_id=f"task-cli-idempotency-{uuid4().hex}",
        task_type="sql_analysis",
        trajectory_id=None,
        config_path=str(cfg),
    )
    first = run_commit(**common)
    second = run_commit(**common)
    assert first["status"] == "accepted"
    assert second["status"] == "idempotent"

    third = run_commit(**common, disable_idempotency=True)
    assert third["status"] == "accepted"

