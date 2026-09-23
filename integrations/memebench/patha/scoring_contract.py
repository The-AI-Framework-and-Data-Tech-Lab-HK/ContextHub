"""Versioned S1 scoring contract and denominator-preservation checks.

This is not the final S4/S8 scorer.  It freezes record membership and the
currently executable pass/fail rules while explicitly reserving the final
scorer code identity for the stage that implements it.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping

from .schemas import SCHEMA_VERSION, canonical_sha256


class ScoringContractError(ValueError):
    pass


def build_scoring_contract(
    records: Iterable[Mapping[str, Any]],
    paths: Iterable[Mapping[str, Any]],
    *,
    artifact_hashes: Mapping[str, str],
    contract_validator_code: Mapping[str, str],
    official_reference: Mapping[str, Any] | None = None,
    contract_version: str = "scorer-contract-v1",
    adjudication_decision: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    record_rows = [dict(row) for row in records]
    path_rows = [dict(row) for row in paths]
    record_ids = [row["record_id"] for row in record_rows]
    path_by_id = {row["record_id"]: row for row in path_rows}
    if len(record_ids) != len(set(record_ids)):
        raise ScoringContractError("scoreable record IDs must be unique")
    if set(record_ids) != set(path_by_id):
        raise ScoringContractError("scoreable records and necessary paths must have identical IDs")
    answer_status = Counter(row["answer_pair_status"] for row in record_rows)
    path_status = Counter(row["path_status"] for row in record_rows)
    contract: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": contract_version,
        "scope": {
            "included_task_types": ["Cas", "Abs"],
            "excluded_task_types": ["Del", "ER", "Agg", "Tr"],
            "record_unit": "complete event-target within an episode",
            "episode_cluster_retained": True,
            "intent_to_treat": True,
            "failure_policy": (
                "timeout, empty output, missing result, provider error, and failed attempts "
                "remain denominator rows"
            ),
        },
        "frozen_record_universe": {
            "record_count": len(record_rows),
            "record_ids": record_ids,
            "answer_pair_status_counts": dict(sorted(answer_status.items())),
            "necessary_path_status_counts": dict(sorted(path_status.items())),
            "unresolved_record_ids": [
                row["record_id"]
                for row in record_rows
                if row["answer_pair_status"] != "scoreable" or row["path_status"] != "scoreable"
            ],
            "unresolved_policy": (
                "none after the 2026-09-23 S1-R3 inclusion decision; all 294 records "
                "remain in mechanism and official-answer scoring, while data-quality "
                "notes remain reportable"
                if adjudication_decision is not None
                else "retain and report; final P1/P2 denominator treatment requires "
                "explicit adjudication and a versioned contract change"
            ),
        },
        "p1": {
            "kappa_P1": 1.0,
            "criterion": (
                "each adjudicated scoreable event-target has at least one complete, "
                "semantically valid, direction-correct path traversable by P2"
            ),
            "path_match": "any one fully valid necessary path is sufficient",
            "invalid_path_examples": ["reversed", "disconnected", "partial"],
            "whole_graph_precision_certified": False,
            "unresolved_labels_are_not_automatic_exclusions": True,
        },
        "p2": {
            "false_fresh_threshold": 0.0,
            "recoverable_cas_recovery_threshold": 0.90,
            "quality_noninferiority_margin": -0.05,
            "false_fresh_main_denominator": "actual_fresh",
            "zero_fresh": "N/A; never coerce to zero or automatic pass",
            "report": [
                "raw false-fresh count",
                "false-fresh over actual fresh when defined",
                "false-fresh over all cases",
                "recoverable Cas correctly restored",
                "recoverable Cas not restored",
                "Abs correctly unresolved",
            ],
            "conditional_result": "only targets delivered by frozen P1",
            "joint_result": "all frozen records, retaining P1 misses",
        },
        "official_answer": {
            "primary_pass": "both paired before and after answers pass official semantics",
            "judge_input": "offline entity_values from scoring sidecar; never maintenance input",
            "do_not_substitute": "gold_answer for entity_values, especially Abs",
            "other_official_result_types": "retain for explanation",
            "reference": dict(official_reference or {}),
        },
        "statistics": {
            "cluster_unit": "episode",
            "bootstrap_repetitions": 10000,
            "bootstrap_seed": "pending formal-run manifest",
            "paired_difference_sign": "candidate minus comparator",
            "intervals": "one-sided 95% bound and two-sided 95% interval",
        },
        "artifact_hashes": dict(sorted(artifact_hashes.items())),
        "s1_validator_code": dict(sorted(contract_validator_code.items())),
        "final_scorer_identity": {
            "status": "pending_s4_s8_implementation",
            "entrypoint": None,
            "code_sha256": None,
            "dependency_hashes": {},
            "required_before_formal_run": True,
            "note": "S1 must not fabricate a hash for code that does not yet exist.",
        },
    }
    if adjudication_decision is not None:
        contract["s1_r3_adjudication"] = dict(adjudication_decision)
    contract["contract_sha256"] = canonical_sha256(contract)
    validate_scoring_contract(contract)
    return contract


def validate_scoring_contract(contract: Mapping[str, Any]) -> None:
    version = contract.get("contract_version")
    if version not in {"scorer-contract-v1", "scorer-contract-v2"}:
        raise ScoringContractError("unsupported scoring contract version")
    expected = contract.get("contract_sha256")
    payload = dict(contract)
    payload.pop("contract_sha256", None)
    if canonical_sha256(payload) != expected:
        raise ScoringContractError("scoring contract hash mismatch")
    universe = contract.get("frozen_record_universe") or {}
    record_ids = list(universe.get("record_ids") or [])
    if universe.get("record_count") != len(record_ids) or len(record_ids) != len(set(record_ids)):
        raise ScoringContractError("frozen record universe is inconsistent")
    final_identity = contract.get("final_scorer_identity") or {}
    if final_identity.get("status") == "pending_s4_s8_implementation":
        if final_identity.get("entrypoint") is not None or final_identity.get("code_sha256") is not None:
            raise ScoringContractError("pending final scorer must not claim an entrypoint or hash")
    if version == "scorer-contract-v2":
        decision = contract.get("s1_r3_adjudication") or {}
        expected_counts = {
            "mechanism_record_count": 294,
            "official_answer_record_count": 294,
            "cas_record_count": 164,
            "abs_record_count": 130,
            "supplemented_dialogue_path_record_count": 11,
        }
        for key, expected_value in expected_counts.items():
            if decision.get(key) != expected_value:
                raise ScoringContractError(
                    f"S1-R3 adjudication {key} must be {expected_value}"
                )
        locked_count = decision.get("forward_locked_record_count")
        development_count = decision.get("development_record_count")
        if (
            not isinstance(locked_count, int)
            or not isinstance(development_count, int)
            or locked_count < 0
            or development_count < 0
            or locked_count + development_count != 294
        ):
            raise ScoringContractError("S1-R3 locked/development record counts are invalid")
        pl_030 = decision.get("pl_030") or {}
        if (
            pl_030.get("decision")
            != "included_in_mechanism_and_official_scoring"
            or pl_030.get("benchmark_reference_path")
            != ["employer", "work_location", "commute_method"]
            or pl_030.get("evidence_status")
            != "benchmark_label_with_data_quality_note"
            or pl_030.get("official_gold_unchanged") is not True
            or pl_030.get("maintenance_input_unchanged") is not True
            or decision.get("data_quality_notes_retained") is not True
        ):
            raise ScoringContractError("S1-R3 pl_030 inclusion decision is incomplete")
        if universe.get("record_count") != 294 or universe.get("unresolved_record_ids"):
            raise ScoringContractError(
                "S1-R3 v2 must include all 294 records with no pending adjudication"
            )


def retain_denominator_failures(
    record_ids: Iterable[str],
    outcomes: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Materialize one output row per frozen record, including missing failures."""

    frozen = list(record_ids)
    if len(frozen) != len(set(frozen)):
        raise ScoringContractError("record IDs must be unique")
    extras = set(outcomes).difference(frozen)
    if extras:
        raise ScoringContractError(f"outcomes contain non-frozen record IDs: {sorted(extras)}")
    rows: list[dict[str, Any]] = []
    for record_id in frozen:
        if record_id not in outcomes:
            rows.append(
                {
                    "record_id": record_id,
                    "outcome": "failed",
                    "failure_reason": "missing_result",
                    "retained_in_denominator": True,
                }
            )
            continue
        row = dict(outcomes[record_id])
        row["record_id"] = record_id
        row["retained_in_denominator"] = True
        rows.append(row)
    return rows
