"""Fail-closed S1 artifact and future-run preflight."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .firewall import validate_maintenance_payload
from .forward_lock import ForwardLockError, validate_forward_lock_manifest
from .prepare import (
    DEFAULT_MATERIAL_VERSION,
    R3_INCLUDED_ANOMALY,
    R3_SUPPLEMENTED_EPISODES,
    SUPPORTED_MATERIAL_VERSIONS,
    _code_hashes,
    artifact_layout,
    sha256_file,
)
from .schemas import AnswerBundle, ScoringSidecar, canonical_sha256
from .scoring_contract import validate_scoring_contract


class PreflightError(RuntimeError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PreflightError(f"expected object in {path}")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    # TextIO iteration splits only on the JSONL LF delimiter.  str.splitlines()
    # also treats U+2028/U+2029 inside legitimate conversation strings as line
    # boundaries and would corrupt those records.
    with path.open(encoding="utf-8", newline="") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise PreflightError(f"blank JSONL line at {path}:{line_number}")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise PreflightError(f"expected object at {path}:{line_number}")
            rows.append(value)
    return rows


def _validate_self_hash(value: Mapping[str, Any], field: str, label: str) -> None:
    payload = dict(value)
    expected = payload.pop(field, None)
    if canonical_sha256(payload) != expected:
        raise PreflightError(f"{label} {field} mismatch")


def assert_forward_lock_ready(manifest: Mapping[str, Any]) -> None:
    try:
        validate_forward_lock_manifest(manifest)
    except ForwardLockError as exc:
        raise PreflightError(f"model/smoke run blocked: {exc}") from exc


def _verify_declared_hashes(root: Path, declared: Mapping[str, str]) -> None:
    for relative, expected in declared.items():
        path = root / relative
        if not path.is_file():
            raise PreflightError(f"declared artifact is missing: {relative}")
        actual = sha256_file(path)
        if actual != expected:
            raise PreflightError(
                f"artifact hash mismatch for {relative}: expected {expected}, got {actual}"
            )


def _prefix_hash(sources: list[dict[str, Any]], end_ordinal: int) -> str:
    return canonical_sha256(
        [
            {
                "source_id": source["source_id"],
                "source_version": source["source_version"],
                "content_sha256": source["content_sha256"],
            }
            for source in sources[: end_ordinal + 1]
        ]
    )


def preflight_artifacts(
    root: Path, *, material_version: str = DEFAULT_MATERIAL_VERSION
) -> dict[str, Any]:
    layout = artifact_layout(material_version)
    required = list(dict.fromkeys(layout.values()))
    missing = [str(relative) for relative in required if not (root / relative).is_file()]
    if missing:
        raise PreflightError(f"S1 artifacts are incomplete: {missing}")

    forward_lock = _load_json(root / layout["forward_lock"])
    assert_forward_lock_ready(forward_lock)
    allowed = _load_json(root / layout["allowed_inputs"])
    _validate_self_hash(allowed, "manifest_sha256", "allowed-inputs manifest")
    if allowed.get("manifest_version") != f"allowed-inputs-{material_version}":
        raise PreflightError("allowed-inputs material version mismatch")
    if allowed.get("generator_code_sha256") != _code_hashes():
        raise PreflightError("S1 generator/validator code identity differs from allowed-inputs manifest")
    contract = _load_json(root / layout["scorer_contract"])
    validate_scoring_contract(contract)
    if contract.get("contract_version") != f"scorer-contract-{material_version}":
        raise PreflightError("scoring contract material version mismatch")
    _verify_declared_hashes(root, contract["artifact_hashes"])

    maintenance_rows = _load_jsonl(root / layout["maintenance"])
    maintenance_by_id: dict[str, dict[str, Any]] = {}
    for row in maintenance_rows:
        model = validate_maintenance_payload(row)
        if model.episode_id in maintenance_by_id:
            raise PreflightError(f"duplicate maintenance episode ID: {model.episode_id}")
        maintenance_by_id[model.episode_id] = model.model_dump(mode="json")
    if len(maintenance_rows) != 100:
        raise PreflightError(f"expected 100 maintenance inputs, got {len(maintenance_rows)}")

    answer_rows = _load_jsonl(root / layout["answers"])
    answer_pairs = Counter()
    question_ids: set[str] = set()
    for row in answer_rows:
        model = AnswerBundle.model_validate(row)
        maintenance = maintenance_by_id.get(model.episode_id)
        if maintenance is None:
            raise PreflightError(f"answer bundle has unknown episode: {model.episode_id}")
        sources = maintenance["sources"]
        if model.release_after_ordinal >= len(sources):
            raise PreflightError("answer release boundary is outside maintenance input")
        source = sources[model.release_after_ordinal]
        if model.release_after_source_id != source["source_id"]:
            raise PreflightError("answer release source does not match ordinal")
        if model.source_prefix_sha256 != _prefix_hash(sources, model.release_after_ordinal):
            raise PreflightError("answer source-prefix hash mismatch")
        answer_pairs[(model.episode_id, model.phase)] += 1
        for question in model.questions:
            if question.question_id in question_ids:
                raise PreflightError(f"duplicate question ID: {question.question_id}")
            question_ids.add(question.question_id)
    if len(answer_rows) != 200 or any(value != 1 for value in answer_pairs.values()):
        raise PreflightError("answer bundles must contain exactly one before and one after bundle per episode")

    scoring_rows = _load_jsonl(root / layout["scoreable"])
    scoring_by_id: dict[str, dict[str, Any]] = {}
    for row in scoring_rows:
        model = ScoringSidecar.model_validate(row)
        if model.record_id in scoring_by_id:
            raise PreflightError(f"duplicate scoring record: {model.record_id}")
        if model.before_question_id not in question_ids or model.after_question_id not in question_ids:
            raise PreflightError(f"scoring record references unknown question: {model.record_id}")
        scoring_by_id[model.record_id] = model.model_dump(mode="json")
    if len(scoring_rows) != 294:
        raise PreflightError(f"expected 294 Cas/Abs scoring rows, got {len(scoring_rows)}")

    path_rows = _load_jsonl(root / layout["paths"])
    path_by_id: dict[str, dict[str, Any]] = {}
    for row in path_rows:
        expected = row.get("record_sha256")
        payload = dict(row)
        payload.pop("record_sha256", None)
        if canonical_sha256(payload) != expected:
            raise PreflightError(f"necessary-path row hash mismatch: {row.get('record_id')}")
        record_id = row.get("record_id")
        if record_id in path_by_id:
            raise PreflightError(f"duplicate necessary-path record: {record_id}")
        path_by_id[record_id] = row
    if set(path_by_id) != set(scoring_by_id):
        raise PreflightError("scoring and necessary-path record universes differ")
    for record_id, scoring in scoring_by_id.items():
        if scoring["path_status"] != path_by_id[record_id]["path_status"]:
            raise PreflightError(f"path status disagreement for {record_id}")

    data_quality_record_ids: set[str] = set()
    if material_version == "v2":
        supplemented_rows: list[dict[str, Any]] = []
        anomaly_rows: list[dict[str, Any]] = []
        for record_id, path_row in path_by_id.items():
            scoring = scoring_by_id[record_id]
            required_inclusion = (
                path_row.get("adjudication_status") == "resolved_included"
                and path_row.get("mechanism_scoring_included") is True
                and path_row.get("official_answer_scoring_included") is True
                and scoring.get("adjudication_status") == "resolved_included"
                and scoring.get("mechanism_scoring_included") is True
                and scoring.get("official_answer_scoring_included") is True
            )
            if not required_inclusion or path_row.get("path_status") != "scoreable":
                raise PreflightError(
                    f"S1-R3 record is not resolved into both score sets: {record_id}"
                )
            notes = list(path_row.get("data_quality_notes") or [])
            if notes != list(scoring.get("data_quality_notes") or []):
                raise PreflightError(f"data-quality notes disagree for {record_id}")
            if notes:
                data_quality_record_ids.add(record_id)
            if any(
                str(note).startswith("dependency_edges_used_incomplete:")
                for note in notes
            ):
                supplemented_rows.append(path_row)
                candidates = path_row.get("necessary_path_candidates") or []
                if len(candidates) != 1 or any(
                    not edge.get("dialogue_evidence")
                    for edge in candidates[0].get("edges") or []
                ):
                    raise PreflightError(
                        f"supplemented path lacks complete original-text evidence: {record_id}"
                    )
            if (
                path_row.get("raw_episode_id"),
                path_row.get("task_index"),
                path_row.get("target_entity"),
            ) == R3_INCLUDED_ANOMALY:
                anomaly_rows.append(path_row)
                if (
                    path_row.get("evidence_status")
                    != "benchmark_label_with_data_quality_note"
                    or not any(
                        str(note).startswith("missing_dialogue_edge_evidence:")
                        for note in notes
                    )
                    or not any(
                        str(note).startswith("original_dialogue_conflict:")
                        for note in notes
                    )
                ):
                    raise PreflightError("pl_030 data-quality annotation is incomplete")
        if len(supplemented_rows) != 11 or {
            row["raw_episode_id"] for row in supplemented_rows
        } != set(R3_SUPPLEMENTED_EPISODES):
            raise PreflightError("S1-R3 must contain exactly the reviewed 11 supplemented paths")
        if len(anomaly_rows) != 1:
            raise PreflightError("S1-R3 must retain exactly one adjudicated pl_030 anomaly")

    if contract["frozen_record_universe"]["record_ids"] != list(scoring_by_id):
        raise PreflightError("scoring contract record order differs from scoreable records")
    locked_ids = {row["episode_id"] for row in forward_lock["forward_locked_episodes"]}
    development_ids = {row["episode_id"] for row in forward_lock["development_episodes"]}
    if locked_ids | development_ids != set(maintenance_by_id):
        raise PreflightError("forward-lock partition differs from maintenance-input episode universe")

    if material_version == "v2":
        decision = contract["s1_r3_adjudication"]
        locked_record_count = sum(
            row["episode_id"] in locked_ids for row in scoring_by_id.values()
        )
        if (
            locked_record_count != decision.get("forward_locked_record_count")
            or len(scoring_by_id) - locked_record_count
            != decision.get("development_record_count")
        ):
            raise PreflightError(
                "S1-R3 scoring records do not match the declared locked/development split"
            )
        if set(decision.get("supplemented_raw_episode_ids") or []) != set(
            R3_SUPPLEMENTED_EPISODES
        ):
            raise PreflightError("S1-R3 contract supplemented episode list mismatch")
        if len(data_quality_record_ids) != 12:
            raise PreflightError("S1-R3 must retain data-quality notes for 12 records")

    path_counts = Counter(row["path_status"] for row in path_rows)
    return {
        "s1_materials_valid": True,
        "forward_lock_valid": True,
        "maintenance_input_count": len(maintenance_rows),
        "answer_bundle_count": len(answer_rows),
        "scoreable_record_count": len(scoring_rows),
        "necessary_path_status_counts": dict(sorted(path_counts.items())),
        "locked_episode_count": len(locked_ids),
        "development_episode_count": len(development_ids),
        "final_scorer_status": contract["final_scorer_identity"]["status"],
        "material_version": material_version,
        "data_quality_record_count": len(data_quality_record_ids),
    }


def assert_model_run_allowed(
    root: Path, *, material_version: str = DEFAULT_MATERIAL_VERSION
) -> dict[str, Any]:
    """Gate any future smoke/model run; S1 intentionally cannot pass it yet."""

    layout = artifact_layout(material_version)
    report = preflight_artifacts(root, material_version=material_version)
    contract = _load_json(root / layout["scorer_contract"])
    blockers: list[str] = []
    if contract["final_scorer_identity"]["status"] != "locked":
        blockers.append("final S4/S8 scorer identity is not locked")
    if contract["frozen_record_universe"]["unresolved_record_ids"]:
        blockers.append("necessary-path records still require explicit adjudication")
    # These identities do not exist in S1 and must not be guessed here.
    blockers.extend(
        [
            "P1 selective replicate_id=0 graph identity is not locked",
            "formal run manifest is not locked",
        ]
    )
    if blockers:
        raise PreflightError("model/smoke run blocked: " + "; ".join(blockers))
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--mode", choices=("s1", "run"), default="s1")
    parser.add_argument(
        "--material-version",
        choices=SUPPORTED_MATERIAL_VERSIONS,
        default=DEFAULT_MATERIAL_VERSION,
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = (
        preflight_artifacts(args.root, material_version=args.material_version)
        if args.mode == "s1"
        else assert_model_run_allowed(
            args.root, material_version=args.material_version
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
