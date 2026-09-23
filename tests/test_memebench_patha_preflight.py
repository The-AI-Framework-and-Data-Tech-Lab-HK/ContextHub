from __future__ import annotations

import json
from pathlib import Path

import pytest

from integrations.memebench.patha.forward_lock import make_forward_lock_manifest
from integrations.memebench.patha.preflight import (
    PreflightError,
    assert_forward_lock_ready,
    assert_model_run_allowed,
    preflight_artifacts,
)
from integrations.memebench.patha.prepare import (
    DEFAULT_DATA_PATH,
    R3_PATHS_REL,
    R3_SUPPLEMENTED_EPISODES,
    prepare_all,
)


@pytest.fixture(scope="module")
def prepared_root(tmp_path_factory) -> Path:
    if not DEFAULT_DATA_PATH.is_file():
        pytest.fail(f"required canonical MEME dataset is missing: {DEFAULT_DATA_PATH}")
    root = tmp_path_factory.mktemp("patha-s1")
    prepare_all(
        data_path=DEFAULT_DATA_PATH,
        output_root=root,
        seed=123456789,
        seed_method="test_seed",
    )
    return root


def test_real_dataset_materials_are_complete_and_separated(prepared_root: Path):
    report = preflight_artifacts(prepared_root)
    assert report["maintenance_input_count"] == 100
    assert report["answer_bundle_count"] == 200
    assert report["scoreable_record_count"] == 294
    assert report["locked_episode_count"] == 20
    assert report["development_episode_count"] == 80
    assert report["material_version"] == "v2"
    assert report["necessary_path_status_counts"] == {"scoreable": 294}
    assert report["data_quality_record_count"] == 12


def test_r3_supplements_eleven_paths_and_keeps_pl030_included(prepared_root: Path):
    rows = [
        json.loads(line)
        for line in (prepared_root / R3_PATHS_REL).read_text(encoding="utf-8").splitlines()
    ]
    supplemented = [
        row
        for row in rows
        if any(
            note.startswith("dependency_edges_used_incomplete:")
            for note in row["data_quality_notes"]
        )
    ]
    assert len(supplemented) == 11
    assert {row["raw_episode_id"] for row in supplemented} == set(
        R3_SUPPLEMENTED_EPISODES
    )
    assert all(
        edge["dialogue_evidence"]
        for row in supplemented
        for edge in row["necessary_path_candidates"][0]["edges"]
    )

    pl_030 = next(
        row
        for row in rows
        if row["raw_episode_id"] == "pl_030"
        and row["task_index"] == 4
        and row["target_entity"] == "commute_method"
    )
    assert pl_030["path_status"] == "scoreable"
    assert pl_030["mechanism_scoring_included"] is True
    assert pl_030["official_answer_scoring_included"] is True
    assert pl_030["evidence_status"] == "benchmark_label_with_data_quality_note"
    assert any(
        note.startswith("missing_dialogue_edge_evidence:")
        for note in pl_030["data_quality_notes"]
    )
    assert any(
        note.startswith("original_dialogue_conflict:")
        for note in pl_030["data_quality_notes"]
    )


def test_future_run_is_blocked_even_after_s1_materials_exist(prepared_root: Path):
    with pytest.raises(PreflightError, match="final S4/S8 scorer") as raised:
        assert_model_run_allowed(prepared_root)
    assert "necessary-path records still require explicit adjudication" not in str(
        raised.value
    )


def test_unlocked_manifest_refuses_startup():
    episodes = [
        {"episode_id": f"pl_{index:03d}", "domain": "personal_life"}
        for index in range(1, 51)
    ] + [
        {"episode_id": f"sw_{index:03d}", "domain": "software_project"}
        for index in range(1, 51)
    ]
    manifest = make_forward_lock_manifest(episodes, dataset_sha256="a" * 64, seed=7)
    manifest["status"] = "draft"
    with pytest.raises(PreflightError, match="not locked"):
        assert_forward_lock_ready(manifest)
