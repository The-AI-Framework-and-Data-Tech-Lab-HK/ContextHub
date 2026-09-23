from __future__ import annotations

import pytest

from integrations.memebench.patha.scoring_contract import (
    ScoringContractError,
    build_scoring_contract,
    retain_denominator_failures,
    validate_scoring_contract,
)


def test_missing_timeout_and_error_rows_remain_in_denominator():
    rows = retain_denominator_failures(
        ["r1", "r2", "r3"],
        {
            "r1": {"outcome": "pass"},
            "r2": {"outcome": "failed", "failure_reason": "timeout"},
        },
    )
    assert [row["record_id"] for row in rows] == ["r1", "r2", "r3"]
    assert rows[1]["failure_reason"] == "timeout"
    assert rows[2]["failure_reason"] == "missing_result"
    assert all(row["retained_in_denominator"] for row in rows)


def test_non_frozen_result_cannot_expand_or_replace_denominator():
    with pytest.raises(ScoringContractError, match="non-frozen"):
        retain_denominator_failures(["r1"], {"r2": {"outcome": "pass"}})


def test_s1_contract_does_not_fabricate_final_scorer_identity():
    records = [
        {
            "record_id": "rec_" + "1" * 24,
            "answer_pair_status": "scoreable",
            "path_status": "needs_adjudication",
        }
    ]
    paths = [{"record_id": records[0]["record_id"]}]
    contract = build_scoring_contract(
        records,
        paths,
        artifact_hashes={"labels.jsonl": "a" * 64},
        contract_validator_code={"scoring_contract.py": "b" * 64},
    )
    assert contract["final_scorer_identity"]["status"] == "pending_s4_s8_implementation"
    assert contract["final_scorer_identity"]["code_sha256"] is None
    assert contract["frozen_record_universe"]["unresolved_record_ids"] == [
        records[0]["record_id"]
    ]
    validate_scoring_contract(contract)


def test_r3_contract_keeps_all_294_records_and_pl030_in_both_score_sets():
    records = [
        {
            "record_id": f"rec_{index:024x}",
            "answer_pair_status": "scoreable",
            "path_status": "scoreable",
        }
        for index in range(294)
    ]
    paths = [{"record_id": row["record_id"]} for row in records]
    decision = {
        "decision_date": "2026-09-23",
        "decision_source": "S1-review.md §6.2 user decision",
        "mechanism_record_count": 294,
        "official_answer_record_count": 294,
        "cas_record_count": 164,
        "abs_record_count": 130,
        "forward_locked_record_count": 58,
        "development_record_count": 236,
        "supplemented_dialogue_path_record_count": 11,
        "supplemented_raw_episode_ids": [],
        "pl_030": {
            "decision": "included_in_mechanism_and_official_scoring",
            "benchmark_reference_path": [
                "employer",
                "work_location",
                "commute_method",
            ],
            "evidence_status": "benchmark_label_with_data_quality_note",
            "official_gold_unchanged": True,
            "maintenance_input_unchanged": True,
        },
        "data_quality_notes_retained": True,
    }
    contract = build_scoring_contract(
        records,
        paths,
        artifact_hashes={"labels.jsonl": "a" * 64},
        contract_validator_code={"scoring_contract.py": "b" * 64},
        contract_version="scorer-contract-v2",
        adjudication_decision=decision,
    )
    assert contract["frozen_record_universe"]["record_count"] == 294
    assert contract["frozen_record_universe"]["unresolved_record_ids"] == []
    assert contract["s1_r3_adjudication"]["mechanism_record_count"] == 294
    assert contract["s1_r3_adjudication"]["official_answer_record_count"] == 294
    validate_scoring_contract(contract)
