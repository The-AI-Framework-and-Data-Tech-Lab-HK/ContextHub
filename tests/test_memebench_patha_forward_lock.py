from __future__ import annotations

from collections import Counter

import pytest

from integrations.memebench.patha.forward_lock import (
    ForwardLockError,
    derive_committed_seed,
    make_forward_lock_manifest,
)


DATA_HASH = "a" * 64


def _episodes() -> list[dict]:
    return [
        {
            "episode_id": f"pl_{index:03d}",
            "domain": "personal_life",
            "tasks": [{"system_score": index}],
        }
        for index in range(1, 51)
    ] + [
        {
            "episode_id": f"sw_{index:03d}",
            "domain": "software_project",
            "tasks": [{"system_score": -index}],
        }
        for index in range(1, 51)
    ]


def test_forward_lock_is_exact_whole_episode_stratified_and_reproducible():
    episodes = _episodes()
    first = make_forward_lock_manifest(episodes, dataset_sha256=DATA_HASH, seed=919)
    second = make_forward_lock_manifest(
        list(reversed(episodes)), dataset_sha256=DATA_HASH, seed=919
    )

    first_ids = {row["raw_episode_id"] for row in first["forward_locked_episodes"]}
    second_ids = {row["raw_episode_id"] for row in second["forward_locked_episodes"]}
    development = {row["raw_episode_id"] for row in first["development_episodes"]}

    assert first_ids == second_ids
    assert len(first_ids) == 20
    assert len(development) == 80
    assert not (first_ids & development)
    assert first_ids | development == {row["episode_id"] for row in episodes}
    assert Counter(row["domain"] for row in first["forward_locked_episodes"]) == {
        "personal_life": 10,
        "software_project": 10,
    }


def test_forward_lock_ignores_system_performance_fields():
    episodes = _episodes()
    before = make_forward_lock_manifest(episodes, dataset_sha256=DATA_HASH, seed=11)
    for episode in episodes:
        episode["tasks"] = [{"system_score": 10_000, "winner": True}]
    after = make_forward_lock_manifest(episodes, dataset_sha256=DATA_HASH, seed=11)
    assert [row["episode_id"] for row in before["forward_locked_episodes"]] == [
        row["episode_id"] for row in after["forward_locked_episodes"]
    ]


def test_seed_is_mandatory_and_commitment_is_stable():
    with pytest.raises(ForwardLockError, match="not confirmed"):
        make_forward_lock_manifest(_episodes(), dataset_sha256=DATA_HASH, seed=None)
    assert derive_committed_seed(DATA_HASH) == derive_committed_seed(DATA_HASH)
    assert 0 <= derive_committed_seed(DATA_HASH) < 2**64
