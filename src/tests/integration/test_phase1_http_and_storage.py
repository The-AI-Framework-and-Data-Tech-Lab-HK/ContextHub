"""Phase 1 integration tests aligned with AMC_plan/13."""

from __future__ import annotations

import json
import os
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
    # Preferred (non-deprecated) commit body shape with account/scope fields.
    raw = json.loads((sample_traj_dir / name).read_text(encoding="utf-8"))
    steps = raw.get("trajectory") if isinstance(raw, dict) else raw
    return {
        "account_id": "account-a",
        "session_id": "session-1",
        "task_id": f"task-{name}",
        "trajectory": steps,
        "labels": {},
        "scope": "agent",
        "owner_space": "agent-1",
        "is_incremental": False,
    }


def _payload_header_mode(sample_traj_dir: Path, name: str = "traj1.json") -> dict:
    raw = json.loads((sample_traj_dir / name).read_text(encoding="utf-8"))
    steps = raw.get("trajectory") if isinstance(raw, dict) else raw
    return {
        "session_id": "session-header",
        "task_id": f"task-header-{name}",
        "trajectory": steps,
        "labels": {},
        "scope": "agent",
        "is_incremental": False,
    }


def _header(account_id: str = "account-a", agent_id: str = "agent-1") -> dict[str, str]:
    return {"X-Account-Id": account_id, "X-Agent-Id": agent_id}


def _retrieve_payload_header_mode() -> dict:
    return {
        "query": {"task_description": "analyze revenue trend", "constraints": {"tool_whitelist": []}},
        "scope": ["agent"],
        "owner_space": ["agent-header"],
        "top_k": 5,
    }


def test_i01_post_commit_accepted(sample_traj_dir: Path, tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    resp = client.post("/api/v1/amc/commit", json=_payload(sample_traj_dir, "traj1.json"), headers=_header())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["nodes"] > 0
    assert body["edges"] >= 0


def test_i02_fs_writes_trajectory_bundle(sample_traj_dir: Path, tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    resp = client.post("/api/v1/amc/commit", json=_payload(sample_traj_dir, "traj2.json"), headers=_header())
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


def _neo4j_ready() -> bool:
    return bool(
        os.environ.get("AMC_NEO4J_URI")
        and os.environ.get("AMC_NEO4J_USER")
        and os.environ.get("AMC_NEO4J_PASSWORD")
    )


@pytest.mark.skipif(not _neo4j_ready(), reason="Neo4j env not set for I-03")
def test_i03_neo4j_raw_clean_graph_kinds(sample_traj_dir: Path, tmp_path: Path) -> None:
    from neo4j import GraphDatabase

    settings = _settings(tmp_path)
    settings.neo4j_uri = os.environ["AMC_NEO4J_URI"]
    settings.neo4j_user = os.environ["AMC_NEO4J_USER"]
    settings.neo4j_password = os.environ["AMC_NEO4J_PASSWORD"]
    settings.neo4j_database = os.environ.get("AMC_NEO4J_DATABASE", "neo4j")

    app = create_app(settings)
    client = TestClient(app)
    body = client.post(
        "/api/v1/amc/commit", json=_payload(sample_traj_dir, "traj1.json"), headers=_header()
    ).json()
    tid = body["trajectory_id"]

    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    try:
        with driver.session(database=settings.neo4j_database) as session:
            traj = session.run(
                "MATCH (t:AMCTrajectory {trajectory_id:$tid}) RETURN count(t) AS c",
                tid=tid,
            ).single()
            assert traj and traj["c"] == 1

            node_counts = session.run(
                """
                MATCH (n:AMCNode {trajectory_id:$tid})
                RETURN
                  count(n) AS total,
                  count(CASE WHEN n:RawNode THEN 1 END) AS raw_count,
                  count(CASE WHEN n:CleanNode THEN 1 END) AS clean_count
                """,
                tid=tid,
            ).single()
            assert node_counts and node_counts["total"] > 0
            assert node_counts["raw_count"] > 0
            assert node_counts["clean_count"] > 0

            rel_counts = session.run(
                """
                MATCH (:AMCNode {trajectory_id:$tid})-[r]->(:AMCNode {trajectory_id:$tid})
                RETURN count(r) AS rel_total,
                       count(CASE WHEN type(r) IN ['DATAFLOW','REASONING','TEMPORAL','RETRY','CONTROLFLOW'] THEN 1 END) AS typed_total
                """,
                tid=tid,
            ).single()
            assert rel_counts and rel_counts["rel_total"] > 0
            assert rel_counts["typed_total"] == rel_counts["rel_total"]
    finally:
        driver.close()


def test_i05_audit_entry_written(sample_traj_dir: Path, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    client = TestClient(app)
    _ = client.post("/api/v1/amc/commit", json=_payload(sample_traj_dir, "traj5.json"), headers=_header())
    audit_file = Path(settings.storage.audit_file_path)
    assert audit_file.exists()
    lines = [ln for ln in audit_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines
    rec = json.loads(lines[-1])
    assert rec["action"] == "commit"
    assert rec["result"] in ("accepted", "idempotent", "accepted_idempotency_disabled")


def test_i06_replay_reads_steps(sample_traj_dir: Path, tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    payload = _payload(sample_traj_dir, "traj3.json")
    commit = client.post("/api/v1/amc/commit", json=payload, headers=_header()).json()
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
    first = client.post("/api/v1/amc/commit", json=payload, headers=_header())
    second = client.post("/api/v1/amc/commit", json=payload, headers=_header())
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "accepted"


def test_commit_header_mode_without_legacy_body_fields(sample_traj_dir: Path, tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    payload = _payload_header_mode(sample_traj_dir, "traj1.json")
    resp = client.post(
        "/api/v1/amc/commit",
        json=payload,
        headers={"X-Account-Id": "acc-header", "X-Agent-Id": "agent-header"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert isinstance(body["warnings"], list)
    assert not any("deprecated" in str(w).lower() for w in body["warnings"])


def test_commit_legacy_body_agent_field_emits_deprecation_warning(
    sample_traj_dir: Path, tmp_path: Path
) -> None:
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    legacy = _payload(sample_traj_dir, "traj2.json")
    legacy.pop("scope", None)
    legacy.pop("owner_space", None)
    legacy["agent_id"] = "agent-1"
    resp = client.post("/api/v1/amc/commit", json=legacy)
    assert resp.status_code == 200
    warnings = [str(x).lower() for x in (resp.json().get("warnings") or [])]
    assert any("body.agent_id is deprecated" in w for w in warnings)


def test_commit_scope_owner_space_must_match_agent_scope_rule(
    sample_traj_dir: Path, tmp_path: Path
) -> None:
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    payload = _payload_header_mode(sample_traj_dir, "traj3.json")
    payload["owner_space"] = "another-agent"
    resp = client.post(
        "/api/v1/amc/commit",
        json=payload,
        headers={"X-Account-Id": "acc-header", "X-Agent-Id": "agent-header"},
    )
    assert resp.status_code == 422
    assert "scope=agent requires owner_space" in str(resp.json().get("detail") or "")


def test_retrieve_header_mode_without_legacy_body_fields(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    resp = client.post(
        "/api/v1/amc/retrieve",
        json=_retrieve_payload_header_mode(),
        headers={"X-Account-Id": "acc-header", "X-Agent-Id": "agent-header"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "warnings" in body
    assert not any("deprecated" in str(w).lower() for w in (body.get("warnings") or []))


def test_retrieve_legacy_body_agent_field_emits_deprecation_warning(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    payload = _retrieve_payload_header_mode()
    payload["agent_id"] = "agent-1"
    resp = client.post("/api/v1/amc/retrieve", json=payload, headers={"X-Account-Id": "acc-header"})
    assert resp.status_code == 200
    warnings = [str(x).lower() for x in (resp.json().get("warnings") or [])]
    assert any("body.agent_id is deprecated" in w for w in warnings)


def test_retrieve_header_body_context_mismatch_returns_422(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    payload = _retrieve_payload_header_mode()
    payload["agent_id"] = "agent-body"
    resp = client.post(
        "/api/v1/amc/retrieve",
        json=payload,
        headers={"X-Account-Id": "acc-header", "X-Agent-Id": "agent-header"},
    )
    assert resp.status_code == 422
    assert "context mismatch" in str(resp.json().get("detail") or "")


def test_promote_agent_trajectory_to_team_scope(sample_traj_dir: Path, tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    payload = _payload(sample_traj_dir, "traj1.json")
    payload["owner_space"] = "agent-a"
    commit = client.post(
        "/api/v1/amc/commit",
        json=payload,
        headers={"X-Account-Id": "account-a", "X-Agent-Id": "agent-a"},
    )
    assert commit.status_code == 200
    tid = str(commit.json()["trajectory_id"])

    promote = client.post(
        "/api/v1/amc/promote",
        json={"trajectory_id": tid, "target_team": "engineering", "reason": "demo reuse"},
        headers={"X-Account-Id": "account-a", "X-Agent-Id": "agent-a"},
    )
    assert promote.status_code == 200
    body = promote.json()
    assert body["trajectory_id"] == tid
    assert body["scope"] == "team"
    assert body["owner_space"] == "engineering"
    assert body["source_uri"].startswith("ctx://agent/agent-a/memories/trajectories/")
    assert body["target_uri"].startswith("ctx://team/engineering/memories/trajectories/")

    promoted_meta = (
        tmp_path
        / "content"
        / "accounts"
        / "account-a"
        / "scope"
        / "team"
        / "engineering"
        / "memories"
        / "trajectories"
        / tid
        / "meta.json"
    )
    assert promoted_meta.exists()


def test_promote_forbidden_when_not_source_owner(sample_traj_dir: Path, tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    payload = _payload(sample_traj_dir, "traj2.json")
    payload["owner_space"] = "agent-a"
    commit = client.post(
        "/api/v1/amc/commit",
        json=payload,
        headers={"X-Account-Id": "account-a", "X-Agent-Id": "agent-a"},
    )
    assert commit.status_code == 200
    tid = str(commit.json()["trajectory_id"])

    promote = client.post(
        "/api/v1/amc/promote",
        json={"trajectory_id": tid, "target_team": "engineering"},
        headers={"X-Account-Id": "account-a", "X-Agent-Id": "agent-b"},
    )
    assert promote.status_code == 403


def test_promote_duplicate_target_overwrites(sample_traj_dir: Path, tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    client = TestClient(app)
    payload = _payload(sample_traj_dir, "traj3.json")
    payload["owner_space"] = "agent-a"
    commit = client.post(
        "/api/v1/amc/commit",
        json=payload,
        headers={"X-Account-Id": "account-a", "X-Agent-Id": "agent-a"},
    )
    assert commit.status_code == 200
    tid = str(commit.json()["trajectory_id"])

    first = client.post(
        "/api/v1/amc/promote",
        json={"trajectory_id": tid, "target_team": "engineering"},
        headers={"X-Account-Id": "account-a", "X-Agent-Id": "agent-a"},
    )
    assert first.status_code == 200
    second = client.post(
        "/api/v1/amc/promote",
        json={"trajectory_id": tid, "target_team": "engineering"},
        headers={"X-Account-Id": "account-a", "X-Agent-Id": "agent-a"},
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["trajectory_id"] == tid
    assert second_body["scope"] == "team"
    assert second_body["owner_space"] == "engineering"
