"""Phase 1 integration tests aligned with AMC_plan/13."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import (
    ApiSection,
    AppSection,
    AppSettings,
    CommitSection,
    ModelEndpointsSection,
    StorageSection,
)
from app.wiring import create_app

pytestmark = [pytest.mark.integration, pytest.mark.m1]


def _settings(tmp_path: Path) -> AppSettings:
    # Use temp directories to keep tests hermetic and side-effect free.
    return AppSettings(
        app=AppSection(),
        api=ApiSection(prefix="/api/v1/amc", max_payload_mb=20),
        commit=CommitSection(max_action_result_chars=12000),
        storage=StorageSection(
            localfs_root=str(tmp_path / "content"),
            event_jsonl_path=str(tmp_path / "events" / "amc_events.jsonl"),
            audit_file_path=str(tmp_path / "audit" / "amc_audit.log"),
        ),
        model_endpoints=ModelEndpointsSection(),
    )


def _settings_idempotency_disabled(tmp_path: Path) -> AppSettings:
    s = _settings(tmp_path)
    s.commit.idempotency_enabled = False
    return s


def _payload(sample_traj_dir: Path, name: str = "traj1.json") -> dict:
    # Minimal commit body shape from AMC_plan/03 API draft.
    steps = json.loads((sample_traj_dir / name).read_text(encoding="utf-8"))
    return {
        "tenant_id": "tenant-a",
        "agent_id": "agent-1",
        "session_id": "session-1",
        "task_id": f"task-{name}",
        "trajectory": steps,
        "labels": {"task_type": "sql_analysis"},
        "is_incremental": False,
    }


def test_i01_post_commit_accepted(sample_traj_dir: Path, tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    resp = client.post("/api/v1/amc/commit", json=_payload(sample_traj_dir, "traj1.json"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["nodes"] > 0
    assert body["edges"] >= 0


def test_i02_fs_writes_trajectory_bundle(sample_traj_dir: Path, tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    resp = client.post("/api/v1/amc/commit", json=_payload(sample_traj_dir, "traj2.json"))
    body = resp.json()
    tid = body["trajectory_id"]

    replay = client.get(f"/api/v1/amc/replay/{tid}")
    assert replay.status_code == 200
    meta = replay.json()["meta"]
    # Infer trajectory folder from graph pointer to assert bundle completeness.
    base = Path(replay.json()["graph_pointer"]["raw_graph_file"]).parent
    assert (base / "trajectory.json").exists()
    assert (base / "graph_pointer.json").exists()
    assert (base / ".abstract.md").exists()
    assert (base / ".overview.md").exists()
    assert meta["trajectory_id"] == tid


@pytest.mark.skip(reason="I-03 requires Neo4j graph backend wiring (Phase 1.1)")
def test_i03_neo4j_raw_clean_graph_kinds() -> None:
    pass


@pytest.mark.skip(reason="I-04 requires Chroma upsert wiring (Phase 1.1)")
def test_i04_chroma_upsert_idempotent() -> None:
    pass


def test_i05_audit_entry_written(sample_traj_dir: Path, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    client = TestClient(app)
    _ = client.post("/api/v1/amc/commit", json=_payload(sample_traj_dir, "traj5.json"))
    audit_file = Path(settings.storage.audit_file_path)
    assert audit_file.exists()
    lines = [ln for ln in audit_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines
    rec = json.loads(lines[-1])
    assert rec["action"] == "commit"
    assert rec["result"] in ("accepted", "idempotent")


def test_i06_replay_reads_steps(sample_traj_dir: Path, tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    payload = _payload(sample_traj_dir, "traj3.json")
    commit = client.post("/api/v1/amc/commit", json=payload).json()
    tid = commit["trajectory_id"]
    replay = client.get(f"/api/v1/amc/replay/{tid}")
    assert replay.status_code == 200
    body = replay.json()
    assert body["trajectory_id"] == tid
    assert isinstance(body["trajectory"], list)
    assert len(body["trajectory"]) == len(payload["trajectory"])


def test_repeated_commit_updates_when_idempotency_disabled(
    sample_traj_dir: Path, tmp_path: Path
) -> None:
    app = create_app(_settings_idempotency_disabled(tmp_path))
    client = TestClient(app)
    payload = _payload(sample_traj_dir, "traj1.json")
    first = client.post("/api/v1/amc/commit", json=payload)
    second = client.post("/api/v1/amc/commit", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "accepted"
