"""`task_type` parameter on the MEME loader (working-notes step 4, block 3).

`extract_cascade_cases` / `_find_question` used to hardcode "Cas". They now take
a `task_type` parameter defaulting to "Cas", so every existing zero-arg call
site is byte-for-byte unchanged -- this file pins that guarantee and exercises
the new `Abs` path plus the exclusion list's own integrity.

Zero API: file reads only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from integrations.memebench.loader import (
    DEFAULT_DATA_PATH,
    extract_cascade_cases,
    load_episodes,
)

_EXCLUDED_PATH = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "memebench"
    / "abs_excluded_episodes.json"
)


def _corpus_available() -> bool:
    return Path(DEFAULT_DATA_PATH).exists()


@pytest.fixture(scope="module")
def episodes():
    if not _corpus_available():
        pytest.skip("MEME corpus not present")
    return load_episodes()


def test_default_call_is_still_cas_and_unchanged(episodes):
    """The default-arg path every existing caller uses must not move."""
    default_cases = extract_cascade_cases(episodes)
    explicit_cases = extract_cascade_cases(episodes, task_type="Cas")
    assert len(default_cases) == 164
    assert [c.target_entity for c in default_cases] == [
        c.target_entity for c in explicit_cases
    ]
    assert [c.gold_answer for c in default_cases] == [
        c.gold_answer for c in explicit_cases
    ]


def test_abs_task_type_yields_130_with_parseable_questions(episodes):
    cases = extract_cascade_cases(episodes, task_type="Abs")
    assert len(cases) == 130
    assert {c.hop for c in cases} == {1, 2}
    sample = cases[0]
    assert sample.after_question is not None
    assert sample.after_question.expected_answer.startswith("Uncertain — previously")
    # before_question carries the plain prior value (trivial-pass style pairing).
    assert sample.before_question is not None
    assert not sample.before_question.expected_answer.startswith("Uncertain")


def test_abs_and_cas_extraction_do_not_interfere(episodes):
    """Calling both task types against the same episode list is side-effect free."""
    cas_before = extract_cascade_cases(episodes, task_type="Cas")
    extract_cascade_cases(episodes, task_type="Abs")
    cas_after = extract_cascade_cases(episodes, task_type="Cas")
    assert [c.gold_answer for c in cas_before] == [c.gold_answer for c in cas_after]


def test_exclusion_list_entries_carry_reason_and_evidence_not_a_bare_list():
    if not _EXCLUDED_PATH.exists():
        pytest.skip("exclusion list not present")
    data = json.loads(_EXCLUDED_PATH.read_text())
    assert isinstance(data["excluded"], list)
    for entry in data["excluded"]:
        assert isinstance(entry["episode_id"], str)
        assert entry["reason"]
        assert entry["evidence"]  # a sentence, not a bare id


def test_exclusion_list_matches_the_pinned_inventory_split():
    if not _EXCLUDED_PATH.exists():
        pytest.skip("exclusion list not present")
    data = json.loads(_EXCLUDED_PATH.read_text())
    excluded_ids = {entry["episode_id"] for entry in data["excluded"]}
    hop1_contradiction = {
        entry["episode_id"]
        for entry in data["excluded"]
        if entry["reason"] == "predeclaration_never_stales"
    }
    no_target = {
        entry["episode_id"]
        for entry in data["excluded"]
        if entry["reason"] == "target_fact_never_uttered"
    }
    assert hop1_contradiction == {
        "pl_009", "pl_041", "pl_044", "pl_047",
        "sw_013", "sw_016", "sw_020", "sw_026", "sw_040", "sw_048",
    }
    assert no_target == {"pl_030"}
    assert excluded_ids == hop1_contradiction | no_target
    # The 8 missing-edge hop2 cases are NOT excluded -- they stay in the
    # denominator as expected-failure cases (see note_on_kept_hop2_edge_gap).
    kept_edge_gap = set(data["note_on_kept_hop2_edge_gap"]["episode_ids"])
    assert kept_edge_gap == {
        "sw_001", "sw_009", "sw_010", "sw_019", "sw_028", "sw_036", "sw_044", "sw_046",
    }
    assert not (kept_edge_gap & excluded_ids)
