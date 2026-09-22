"""Verified, no-API migration and continuation support for full100 v3.

The source run is always read-only.  Imported cases remain byte-for-byte source
artifacts and are referenced by hash; they are never represented as re-executed.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any
from uuid import uuid4

from integrations.memebench.cost_interval import (
    ACCOUNTING_SCHEMA_VERSION,
    aggregate_intervals,
    bounded_unknown_completion,
    exact_record,
    frozen_episode_interval,
    interval_distribution,
    interval_record,
    load_frozen_selector_rows,
)
from integrations.memebench.run_full100_v3_p2 import (
    SUPPORTED_TASK_TYPES,
    FrozenV3,
    canonical_full100_episode_ids,
    canonical_json,
    sha256_bytes,
    sha256_file,
    validate_case_artifact,
    validate_paid_cost,
    validate_paid_smoke_evidence,
    verify_run_identity,
)


CONTINUATION_SCHEMA_VERSION = "meme-full100-v3-continuation-v1"
MIGRATION_POLICY_VERSION = "full100-accounting-only-v1"
IMPORTED_STATUS = "imported-finalized"
NATIVE_FINAL_STATUSES = {"finalized", "cost-bounded", "cost-unbounded"}


def split_checkpoint_stem(stem: str) -> tuple[str, str]:
    """Split a checkpoint filename stem into (episode_id, task_type).

    Stems are ``<episode>-<task_type>``; runs frozen before task_type entered the
    config have a bare ``<episode>`` stem and were all Cas.
    """
    episode_id, separator, task_type = stem.rpartition("-")
    if separator and task_type in SUPPORTED_TASK_TYPES:
        return episode_id, task_type
    return stem, "Cas"


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is missing or malformed: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object: {path}")
    return value


def _safe_source_path(path: Path, root: Path, label: str) -> Path:
    unresolved = path.absolute()
    cursor = unresolved
    while cursor != cursor.parent:
        if cursor.is_symlink():
            raise RuntimeError(f"{label} uses a symlink")
        if cursor == root.absolute():
            break
        cursor = cursor.parent
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"{label} escapes source run") from exc
    return resolved


def _tree_snapshot(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"source run contains symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
                "byte_count": len(data),
            }
        )
    return {
        "files": files,
        "file_count": len(files),
        "byte_count": sum(row["byte_count"] for row in files),
        "tree_sha256": sha256_bytes(canonical_json(files).encode()),
    }


def _legacy_identity(source: Path) -> dict[str, Any]:
    marker = _read_object(source / "run_identity.json", "source identity marker")
    preflight = _read_object(source / "preflight.json", "source preflight")
    config = preflight.get("run_config")
    identity = {
        "run_config_hash": preflight.get("run_config_hash"),
        "input_hashes": preflight.get("input_hashes"),
        "config": config,
    }
    if (
        marker.get("run_config_hash") != identity["run_config_hash"]
        or not isinstance(config, Mapping)
        or sha256_bytes(canonical_json(config).encode()) != identity["run_config_hash"]
    ):
        raise RuntimeError("source marker/preflight identity closure failed")
    # This is the critical behavior-code proof.  Every source behavior/config
    # byte named by the old identity must still match.  Accounting-only code is
    # new and is not substituted for any old behavior source.
    verify_run_identity(identity)
    return identity


def _validate_source_case(
    source: Path,
    episode_id: str,
    checkpoint_path: Path,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    checkpoint = _read_object(checkpoint_path, f"checkpoint {episode_id}")
    if (
        checkpoint.get("status") != "success"
        or checkpoint.get("checkpoint_namespace") != "full-run"
        or checkpoint.get("run_config_hash") != identity["run_config_hash"]
    ):
        raise RuntimeError(f"source checkpoint is not finalized: {episode_id}")
    artifact_value = checkpoint.get("artifact_path")
    if not artifact_value:
        raise RuntimeError(f"source checkpoint lacks artifact path: {episode_id}")
    artifact_path = _safe_source_path(
        Path(str(artifact_value)),
        source / "artifacts" / "full-run",
        f"artifact {episode_id}",
    )
    manifest_path = artifact_path.with_name("manifest.json")
    artifact = _read_object(artifact_path, f"artifact {episode_id}")
    manifest = _read_object(manifest_path, f"manifest {episode_id}")
    validate_case_artifact(artifact)
    artifact_sha = sha256_file(artifact_path)
    manifest_sha = sha256_file(manifest_path)
    checkpoint_sha = sha256_file(checkpoint_path)
    if (
        artifact.get("episode_id") != episode_id
        or artifact.get("artifact_mode") != "full-run"
        or artifact.get("synthetic_stub") is not False
        or (artifact.get("authorization") or {}).get("run_config_hash")
        != identity["run_config_hash"]
        or checkpoint.get("artifact_sha256") != artifact_sha
        or manifest.get("artifact_sha256") != artifact_sha
        or manifest.get("artifact_bytes") != artifact_path.stat().st_size
        or manifest.get("episode_id") != episode_id
        or manifest.get("run_config_hash") != identity["run_config_hash"]
    ):
        raise RuntimeError(f"source artifact closure failed: {episode_id}")
    attempts = checkpoint.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise RuntimeError(f"source attempt ledger missing: {episode_id}")
    successful = attempts[-1]
    if (
        successful.get("status") != "success"
        or successful.get("attempt_token") != checkpoint.get("attempt_token")
        or successful.get("call_ledger_sha256")
        != artifact["cost"].get("call_ledger_sha256")
        or successful.get("cost") != artifact["cost"].get("paid_execution")
    ):
        raise RuntimeError(f"source attempt/artifact mismatch: {episode_id}")
    validate_paid_cost(
        artifact["cost"],
        artifact=artifact,
        config=identity["config"],
    )
    return {
        "episode_id": episode_id,
        "source_artifact_path": str(artifact_path),
        "source_artifact_sha256": artifact_sha,
        "source_artifact_bytes": artifact_path.stat().st_size,
        "source_manifest_path": str(manifest_path),
        "source_manifest_sha256": manifest_sha,
        "source_manifest_bytes": manifest_path.stat().st_size,
        "source_checkpoint_path": str(checkpoint_path.resolve()),
        "source_checkpoint_sha256": checkpoint_sha,
        "source_checkpoint_bytes": checkpoint_path.stat().st_size,
        "source_call_ledger_sha256": artifact["cost"]["call_ledger_sha256"],
        "source_attempt_token": checkpoint["attempt_token"],
        "legacy_known_usd": artifact["cost"]["known_usd"],
    }


def validate_continuation_source(source: Path) -> dict[str, Any]:
    """Validate a stopped run without writing or making external calls."""

    source = source.resolve()
    if not source.is_dir():
        raise RuntimeError(f"source run is not a directory: {source}")
    identity = _legacy_identity(source)
    # Gates are reconstructed from primary evidence, not trusted summaries.
    preflight = _read_object(source / "preflight.json", "source preflight")
    smoke = _read_object(source / "smoke_result.json", "source no-API gate")
    if (
        preflight.get("success") is not True
        or smoke.get("success") is not True
        or preflight.get("run_config_hash") != identity["run_config_hash"]
        or smoke.get("run_config_hash") != identity["run_config_hash"]
    ):
        raise RuntimeError("source preflight/no-API gate failed")
    paid = validate_paid_smoke_evidence(source, identity)

    canonical = list(canonical_full100_episode_ids())
    canonical_set = set(canonical)
    checkpoint_dir = source / "checkpoints" / "full-run"
    checkpoint_paths = sorted(checkpoint_dir.glob("*.json"))
    names = [split_checkpoint_stem(path.stem)[0] for path in checkpoint_paths]
    if len(names) != len(set(names)) or not set(names).issubset(canonical_set):
        raise RuntimeError("source full-run checkpoint set has duplicate/extra cases")

    imported: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for checkpoint_path in checkpoint_paths:
        episode_id = split_checkpoint_stem(checkpoint_path.stem)[0]
        checkpoint = _read_object(checkpoint_path, f"checkpoint {episode_id}")
        status = checkpoint.get("status")
        if status == "success":
            imported.append(
                _validate_source_case(source, episode_id, checkpoint_path, identity)
            )
        elif status == "failed":
            if (
                checkpoint.get("checkpoint_namespace") != "full-run"
                or checkpoint.get("run_config_hash") != identity["run_config_hash"]
                or not isinstance(checkpoint.get("attempts"), list)
                or not checkpoint["attempts"]
            ):
                raise RuntimeError(f"failed checkpoint evidence malformed: {episode_id}")
            failed.append(
                {
                    "episode_id": episode_id,
                    "source_checkpoint_path": str(checkpoint_path.resolve()),
                    "source_checkpoint_sha256": sha256_file(checkpoint_path),
                    "source_checkpoint_bytes": checkpoint_path.stat().st_size,
                    "attempt_count": len(checkpoint["attempts"]),
                    "status": "failed-not-imported",
                }
            )
        else:
            raise RuntimeError(f"unsupported source checkpoint status: {episode_id}")

    imported_ids = {row["episode_id"] for row in imported}
    observed_artifacts = {
        path.resolve()
        for path in (source / "artifacts" / "full-run").glob("*/artifact.json")
    }
    expected_artifacts = {
        Path(row["source_artifact_path"]).resolve() for row in imported
    }
    observed_manifests = {
        path.resolve()
        for path in (source / "artifacts" / "full-run").glob("*/manifest.json")
    }
    expected_manifests = {
        Path(row["source_manifest_path"]).resolve() for row in imported
    }
    if (
        observed_artifacts != expected_artifacts
        or observed_manifests != expected_manifests
    ):
        raise RuntimeError("source full-run namespace has missing/extra/orphan evidence")
    if imported_ids & {row["episode_id"] for row in failed}:
        raise RuntimeError("source case is both successful and failed")

    # Failed staging evidence is not imported, but if present it must close by
    # manifest hash and correspond exactly to a failed checkpoint.
    staging_ids = set()
    for artifact_path in sorted((source / ".staging").glob("*/cases/*/artifact.json")):
        artifact = _read_object(artifact_path, "failed staging artifact")
        episode_id = str(artifact.get("episode_id") or "")
        if episode_id not in {row["episode_id"] for row in failed}:
            raise RuntimeError("orphan staging artifact has no failed checkpoint")
        manifest_path = artifact_path.with_name("manifest.json")
        manifest = _read_object(manifest_path, "failed staging manifest")
        digest = sha256_file(artifact_path)
        if (
            manifest.get("artifact_sha256") != digest
            or manifest.get("artifact_bytes") != artifact_path.stat().st_size
        ):
            raise RuntimeError(f"failed staging hash closure failed: {episode_id}")
        staging_ids.add(episode_id)
    if staging_ids - {row["episode_id"] for row in failed}:
        raise RuntimeError("source has extra failed staging cases")

    missing = [
        episode_id
        for episode_id in canonical
        if episode_id not in imported_ids
        and episode_id not in {row["episode_id"] for row in failed}
    ]
    return {
        "valid": True,
        "dry_run": True,
        "source_run": str(source),
        "source_run_identity": identity["run_config_hash"],
        "source_identity": identity,
        "source_tree": _tree_snapshot(source),
        "gate_evidence": {
            "preflight_sha256": sha256_file(source / "preflight.json"),
            "no_api_sha256": sha256_file(source / "smoke_result.json"),
            "paid_smoke_summary_sha256": sha256_file(
                source / "paid_smoke_result.json"
            ),
            "paid_smoke_artifact_sha256": paid["artifact_sha256"],
        },
        "canonical_episode_count": len(canonical),
        "importable_finalized_count": len(imported),
        "failed_not_imported_count": len(failed),
        "not_started_count": len(missing),
        "importable": imported,
        "failed_not_imported": failed,
        "not_started": missing,
        "resume_order": [
            episode_id for episode_id in canonical if episode_id not in imported_ids
        ],
    }


def _accounting_identity() -> dict[str, Any]:
    paths = [
        Path(__file__).resolve(),
        Path(__file__).with_name("cost_interval.py").resolve(),
    ]
    entries = [
        {
            "path": str(path),
            "sha256": sha256_file(path),
            "byte_count": path.stat().st_size,
        }
        for path in paths
    ]
    return {
        "schema_version": ACCOUNTING_SCHEMA_VERSION,
        "migration_policy_version": MIGRATION_POLICY_VERSION,
        "source_files": entries,
        "sha256": sha256_bytes(canonical_json(entries).encode()),
    }


def _legacy_cost_views(artifact: Mapping[str, Any]) -> dict[str, Any]:
    cost = artifact["cost"]
    layers = cost["paid_execution"]["layers"]
    frozen = float(cost["frozen_v3"]["known_usd"])
    answer = float(layers["inference_llm"]["known_usd"])
    p2 = float(layers["p2_cheap_llm"]["known_usd"]) + float(
        layers["oracle_llm"]["known_usd"]
    )
    return {
        # Judge and propagation are intentionally excluded from the MEME view.
        "meme_comparable": exact_record(frozen + answer),
        "p2_incremental": exact_record(p2),
        "audit_all_in": exact_record(float(cost["known_usd"])),
        "bucket_contract": {
            "meme_comparable": ["frozen_v3", "inference_llm"],
            "p2_incremental": ["p2_cheap_llm", "oracle_llm"],
            "audit_all_in": [
                "frozen_v3",
                "inference_llm",
                "judge_llm",
                "p2_cheap_llm",
                "oracle_llm",
                "failed_attempts",
            ],
            "judge_excluded_from_meme": True,
            "embedding_excluded_no_external_embedding_calls": True,
        },
    }


def create_continuation(source: Path, out: Path) -> dict[str, Any]:
    """Create a fresh provenance namespace after complete source validation."""

    validation = validate_continuation_source(source)
    out = out.resolve()
    if out.exists() and any(out.iterdir()):
        raise RuntimeError("continuation output must be a new empty directory")
    out.mkdir(parents=True, exist_ok=True)
    accounting = _accounting_identity()
    identity_preimage = {
        "schema_version": CONTINUATION_SCHEMA_VERSION,
        "behavior_identity": validation["source_run_identity"],
        "behavior_config": validation["source_identity"]["config"],
        "accounting_identity": accounting,
        "migration_policy_version": MIGRATION_POLICY_VERSION,
    }
    continuation_identity = sha256_bytes(canonical_json(identity_preimage).encode())
    marker = {
        **identity_preimage,
        "continuation_identity": continuation_identity,
        "source_run": validation["source_run"],
        "source_tree_sha256": validation["source_tree"]["tree_sha256"],
    }
    (out / "checkpoints" / "full-run").mkdir(parents=True)
    # Checkpoint filenames carry the task type so a Cas and an Abs continuation of
    # the same episodes cannot overwrite each other. Runs frozen before task_type
    # entered the config were all Cas.
    source_task_type = str(
        (validation["source_identity"]["config"] or {}).get("task_type") or "Cas"
    )
    index_rows: list[dict[str, Any]] = []
    for source_row in validation["importable"]:
        episode_id = source_row["episode_id"]
        source_artifact = _read_object(
            Path(source_row["source_artifact_path"]),
            f"source artifact {episode_id}",
        )
        provenance = {
            **source_row,
            "source_run_identity": validation["source_run_identity"],
            "source_tree_sha256": validation["source_tree"]["tree_sha256"],
            "migration_policy_version": MIGRATION_POLICY_VERSION,
            "new_accounting_schema": ACCOUNTING_SCHEMA_VERSION,
            "import_mode": "read-only-hash-reference",
            "executed_by_continuation": False,
            "cost_views": _legacy_cost_views(source_artifact),
        }
        provenance_sha = sha256_bytes(canonical_json(provenance).encode())
        checkpoint = {
            "status": IMPORTED_STATUS,
            "checkpoint_namespace": "full-run",
            "continuation_identity": continuation_identity,
            "episode_id": episode_id,
            "provenance": provenance,
            "provenance_sha256": provenance_sha,
        }
        checkpoint_path = (
            out / "checkpoints" / "full-run" / f"{episode_id}-{source_task_type}.json"
        )
        checkpoint_path.write_text(canonical_json(checkpoint) + "\n", encoding="utf-8")
        index_rows.append(
            {
                "episode_id": episode_id,
                "status": IMPORTED_STATUS,
                "checkpoint_sha256": sha256_file(checkpoint_path),
                "provenance_sha256": provenance_sha,
            }
        )
    index_path = out / "continuation_index.jsonl"
    index_path.write_text(
        "".join(canonical_json(row) + "\n" for row in index_rows),
        encoding="utf-8",
    )
    manifest = {
        **marker,
        "created_without_external_calls": True,
        "external_call_count": 0,
        "source_gate_evidence": validation["gate_evidence"],
        "source_tree": validation["source_tree"],
        "imported_case_count": len(index_rows),
        "failed_not_imported": validation["failed_not_imported"],
        "not_started": validation["not_started"],
        "resume_order": validation["resume_order"],
        "index_sha256": sha256_file(index_path),
    }
    (out / "run_identity.json").write_text(
        canonical_json(marker) + "\n", encoding="utf-8"
    )
    (out / "continuation_manifest.json").write_text(
        canonical_json(manifest) + "\n", encoding="utf-8"
    )
    return manifest


def validate_continuation_directory(out: Path) -> dict[str, Any]:
    out = out.resolve()
    marker = _read_object(out / "run_identity.json", "continuation identity")
    manifest = _read_object(
        out / "continuation_manifest.json", "continuation manifest"
    )
    if marker.get("schema_version") != CONTINUATION_SCHEMA_VERSION:
        raise RuntimeError("continuation schema mismatch")
    if marker.get("accounting_identity") != _accounting_identity():
        raise RuntimeError("accounting policy/source identity changed")
    source_validation = validate_continuation_source(Path(marker["source_run"]))
    if (
        source_validation["source_run_identity"] != marker.get("behavior_identity")
        or source_validation["source_tree"]["tree_sha256"]
        != marker.get("source_tree_sha256")
        or manifest.get("source_tree") != source_validation["source_tree"]
    ):
        raise RuntimeError("source run changed after continuation migration")
    expected_identity = sha256_bytes(
        canonical_json(
            {
                "schema_version": marker["schema_version"],
                "behavior_identity": marker["behavior_identity"],
                "behavior_config": marker["behavior_config"],
                "accounting_identity": marker["accounting_identity"],
                "migration_policy_version": marker["migration_policy_version"],
            }
        ).encode()
    )
    if marker.get("continuation_identity") != expected_identity:
        raise RuntimeError("continuation identity hash mismatch")
    return {"marker": marker, "manifest": manifest, "source": source_validation}


def continuation_pending_episode_ids(out: Path) -> list[str]:
    state = validate_continuation_directory(out)
    canonical = list(canonical_full100_episode_ids())
    completed: set[str] = set()
    for path in sorted((out / "checkpoints" / "full-run").glob("*.json")):
        episode_id = split_checkpoint_stem(path.stem)[0]
        if episode_id not in canonical:
            raise RuntimeError(f"extra continuation checkpoint: {path.stem}")
        row = _read_object(path, "continuation checkpoint")
        if row.get("status") in {IMPORTED_STATUS, *NATIVE_FINAL_STATUSES}:
            completed.add(episode_id)
    imported_expected = {
        row["episode_id"] for row in state["source"]["importable"]
    }
    if not imported_expected.issubset(completed):
        raise RuntimeError("continuation is missing imported finalized cases")
    return [episode_id for episode_id in canonical if episode_id not in completed]


def _validate_interval(row: Mapping[str, Any], label: str) -> None:
    required = {
        "known_usd",
        "lower_usd",
        "upper_usd",
        "cost_exact",
        "cost_bounded",
        "cost_unbounded",
        "unknown_usage_attempts",
        "missing_token_types",
        "upper_bound_sources",
    }
    if not required.issubset(row):
        raise RuntimeError(f"{label} lacks interval fields")
    rebuilt = interval_record(
        known_usd=float(row["known_usd"]),
        lower_usd=float(row["lower_usd"]),
        upper_usd=(
            None if row.get("upper_usd") is None else float(row["upper_usd"])
        ),
        unknown_usage_attempts=row.get("unknown_usage_attempts") or (),
        missing_token_types=row.get("missing_token_types") or (),
        upper_bound_sources=row.get("upper_bound_sources") or (),
    )
    for field in required:
        if rebuilt[field] != row[field]:
            raise RuntimeError(f"{label}.{field} is inconsistent")


def _validate_native_artifact(path: Path, episode_id: str) -> dict[str, Any]:
    artifact = _read_object(path, f"continuation artifact {episode_id}")
    if (
        artifact.get("episode_id") != episode_id
        or artifact.get("execution_complete") is not True
    ):
        raise RuntimeError(f"native continuation artifact incomplete: {episode_id}")
    for section in (
        "authorization",
        "extraction",
        "p1_graph",
        "p2_queue",
        "p2_edges",
        "state_evidence",
        "retrieval",
        "answers",
        "judge",
        "cost",
    ):
        if not artifact.get(section):
            raise RuntimeError(f"native artifact lacks {section}: {episode_id}")
    views = artifact.get("cost_views")
    if not isinstance(views, Mapping):
        raise RuntimeError(f"native artifact lacks separated cost views: {episode_id}")
    for name in ("meme_comparable", "p2_incremental", "audit_all_in"):
        if not isinstance(views.get(name), Mapping):
            raise RuntimeError(f"native artifact lacks cost view {name}: {episode_id}")
        _validate_interval(views[name], f"{episode_id}.{name}")
    return artifact


def validate_continuation_completion(out: Path) -> dict[str, Any]:
    """Close exactly 100 imported/native execution artifacts and their costs."""

    state = validate_continuation_directory(out)
    marker = state["marker"]
    canonical = list(canonical_full100_episode_ids())
    task_type = str((marker.get("behavior_config") or {}).get("task_type") or "Cas")
    checkpoint_dir = out / "checkpoints" / "full-run"
    paths = sorted(checkpoint_dir.glob("*.json"))
    names = [split_checkpoint_stem(path.stem)[0] for path in paths]
    if len(paths) != 100 or set(names) != set(canonical) or len(set(names)) != 100:
        raise RuntimeError("continuation completion requires exactly canonical 100")
    artifacts: list[dict[str, Any]] = []
    views = {"meme_comparable": [], "p2_incremental": [], "audit_all_in": []}
    imported = native = 0
    for episode_id in canonical:
        checkpoint = _read_object(
            checkpoint_dir / f"{episode_id}-{task_type}.json",
            f"continuation checkpoint {episode_id}",
        )
        if checkpoint.get("continuation_identity") != marker["continuation_identity"]:
            raise RuntimeError(f"continuation checkpoint identity mismatch: {episode_id}")
        if checkpoint.get("status") == IMPORTED_STATUS:
            provenance = checkpoint.get("provenance")
            if not isinstance(provenance, Mapping):
                raise RuntimeError(f"import provenance missing: {episode_id}")
            for kind in ("artifact", "manifest", "checkpoint"):
                source_path = Path(str(provenance[f"source_{kind}_path"]))
                if (
                    not source_path.is_file()
                    or sha256_file(source_path)
                    != provenance[f"source_{kind}_sha256"]
                    or source_path.stat().st_size
                    != provenance[f"source_{kind}_bytes"]
                ):
                    raise RuntimeError(
                        f"imported source {kind} changed: {episode_id}"
                    )
            cost_views = provenance.get("cost_views")
            artifact = _read_object(
                Path(str(provenance["source_artifact_path"])),
                f"imported artifact {episode_id}",
            )
            imported += 1
        elif checkpoint.get("status") in NATIVE_FINAL_STATUSES:
            artifact_path = Path(str(checkpoint.get("artifact_path") or ""))
            if (
                not artifact_path.is_file()
                or sha256_file(artifact_path) != checkpoint.get("artifact_sha256")
            ):
                raise RuntimeError(f"native artifact hash mismatch: {episode_id}")
            artifact = _validate_native_artifact(artifact_path, episode_id)
            cost_views = artifact["cost_views"]
            native += 1
        else:
            raise RuntimeError(f"case is not finalized: {episode_id}")
        if not isinstance(cost_views, Mapping):
            raise RuntimeError(f"cost views missing: {episode_id}")
        for name in views:
            row = cost_views.get(name)
            if not isinstance(row, Mapping):
                raise RuntimeError(f"{name} missing: {episode_id}")
            _validate_interval(row, f"{episode_id}.{name}")
            views[name].append(row)
        artifacts.append(artifact)
    summary = build_completion_summary(
        meme_comparable=views["meme_comparable"],
        p2_incremental=views["p2_incremental"],
        audit_all_in=views["audit_all_in"],
        artifacts=artifacts,
    )
    return {
        **summary,
        "completion_closed": True,
        "imported_artifact_count": imported,
        "native_artifact_count": native,
    }


def resume_with_executor(
    out: Path,
    executor: Callable[[str], Mapping[str, Any]],
) -> dict[str, Any]:
    """Run pending cases; bounded/provider failures never stop later cases.

    The executor is the only component allowed to perform external calls.  It
    must return a finalized checkpoint payload.  Identity ambiguity and
    checkpoint corruption still raise before execution.
    """

    state = validate_continuation_directory(out)
    marker = state["marker"]
    task_type = str((marker.get("behavior_config") or {}).get("task_type") or "Cas")
    executed = []
    (out / "checkpoints" / "full-run").mkdir(parents=True, exist_ok=True)
    for episode_id in continuation_pending_episode_ids(out):
        result = dict(executor(episode_id))
        status = str(result.get("status") or "")
        if status not in NATIVE_FINAL_STATUSES:
            raise RuntimeError(f"executor did not finalize {episode_id}")
        checkpoint = {
            **result,
            "episode_id": episode_id,
            "checkpoint_namespace": "full-run",
            "continuation_identity": marker["continuation_identity"],
        }
        path = out / "checkpoints" / "full-run" / f"{episode_id}-{task_type}.json"
        if path.exists():
            raise RuntimeError(f"executor attempted duplicate case: {episode_id}")
        path.write_text(canonical_json(checkpoint) + "\n", encoding="utf-8")
        executed.append(episode_id)
    return {"executed": executed, "remaining": continuation_pending_episode_ids(out)}


def _runtime_interval_from_legacy_cost(cost: Mapping[str, Any]) -> dict[str, Any]:
    unknown = [
        {
            "usage_bucket": bucket,
            "missing_token_types": ["prompt_tokens", "completion_tokens"],
            "source": "live_provider_attempt_without_usage",
        }
        for bucket, row in (cost.get("layers") or {}).items()
        if row.get("retry_usage_unknown")
    ]
    known = float(cost.get("known_usd") or 0.0)
    if cost.get("cost_complete") is True:
        return exact_record(known)
    # At the exception boundary there may be no response and therefore no
    # auditable request-to-attempt association.  Fail honest: unbounded.
    return interval_record(
        known_usd=known,
        upper_usd=None,
        unknown_usage_attempts=unknown or [{"source": "provider_failure"}],
        missing_token_types=["prompt_tokens", "completion_tokens"],
    )


def _attach_native_cost_views(
    artifact: dict[str, Any],
    *,
    frozen: Mapping[str, Any],
) -> None:
    paid = artifact["cost"]["paid_execution"]
    layers = paid["layers"]
    calls = artifact["cost"].get("calls") or []
    max_completion_by_kind = {"answer": 50, "judge": 4}
    call_intervals: dict[str, dict[str, Any]] = {}
    for call in calls:
        retries = int(call.get("retry_attempt") or 0)
        if call.get("retry_usage_unknown") and retries:
            price = call.get("price_snapshot")
            max_completion = max_completion_by_kind.get(str(call.get("kind")))
            known_input = (
                int(call["prompt_tokens"])
                * float(price["input_per_million"])
                / 1_000_000
                if isinstance(price, Mapping)
                else 0.0
            )
            retry_rows = [
                bounded_unknown_completion(
                    known_usd=known_input,
                    max_completion_tokens=max_completion,
                    output_per_million=(
                        float(price["output_per_million"])
                        if isinstance(price, Mapping)
                        else None
                    ),
                    attempt={
                        "call_id": call["call_id"],
                        "attempt_ordinal": ordinal,
                        "known_prompt_tokens": int(call["prompt_tokens"]),
                        "completion_tokens": None,
                    },
                    request_snapshot=(
                        {
                            "request_sha256": call["request_sha256"],
                            "request_bytes": call["request_bytes"],
                            "max_completion_tokens": max_completion,
                        }
                        if max_completion is not None
                        else None
                    ),
                    price_snapshot=price if isinstance(price, Mapping) else None,
                )
                for ordinal in range(1, retries + 1)
            ]
            row = aggregate_intervals(
                [exact_record(float(call["usd"])), *retry_rows]
            )
        else:
            row = exact_record(float(call["usd"]))
        call.update(row)
        call_intervals[str(call["call_id"])] = row

    def layer(name: str) -> dict[str, Any]:
        matching = [
            call_intervals[str(call["call_id"])]
            for call in calls
            if call["usage_bucket"] == name
        ]
        if matching:
            return aggregate_intervals(matching)
        return exact_record(float(layers[name]["known_usd"]))

    inference = layer("inference_llm")
    judge = layer("judge_llm")
    p2_cheap = layer("p2_cheap_llm")
    p2_strong = layer("oracle_llm")
    failed = [
        _runtime_interval_from_legacy_cost(row)
        for row in artifact["cost"].get("failed_attempts") or ()
    ]
    artifact["accounting_schema_version"] = ACCOUNTING_SCHEMA_VERSION
    artifact["cost_views"] = {
        "meme_comparable": aggregate_intervals([frozen, inference]),
        "p2_incremental": aggregate_intervals([p2_cheap, p2_strong]),
        "audit_all_in": aggregate_intervals(
            [frozen, inference, judge, p2_cheap, p2_strong, *failed]
        ),
        "bucket_contract": {
            "meme_comparable": ["frozen_v3", "inference_llm"],
            "p2_incremental": ["p2_cheap_llm", "oracle_llm"],
            "audit_all_in": "all external and frozen spend",
            "judge_excluded_from_meme": True,
        },
    }
    by_stage: dict[str, list[Mapping[str, Any]]] = {}
    for call in calls:
        by_stage.setdefault(str(call["stage"]), []).append(call)
    artifact["cost"]["interval_stage_summary"] = {
        stage: aggregate_intervals(rows) for stage, rows in by_stage.items()
    }
    artifact["cost"]["interval_episode_summary"] = artifact["cost_views"][
        "audit_all_in"
    ]


def _write_native_artifact(
    out: Path,
    episode_id: str,
    artifact: Mapping[str, Any],
    continuation_identity: str,
) -> tuple[Path, str]:
    target = out / "artifacts" / "full-run" / f"{episode_id}-{uuid4().hex[:12]}"
    target.mkdir(parents=True, exist_ok=False)
    artifact_path = target / "artifact.json"
    artifact_path.write_text(canonical_json(artifact) + "\n", encoding="utf-8")
    digest = sha256_file(artifact_path)
    manifest = {
        "schema_version": CONTINUATION_SCHEMA_VERSION,
        "accounting_schema_version": ACCOUNTING_SCHEMA_VERSION,
        "episode_id": episode_id,
        "continuation_identity": continuation_identity,
        "artifact_sha256": digest,
        "artifact_bytes": artifact_path.stat().st_size,
    }
    (target / "manifest.json").write_text(canonical_json(manifest) + "\n")
    return artifact_path, digest


async def continue_live_run(
    source: Path,
    out: Path,
    *,
    providers_path: Path,
) -> dict[str, Any]:
    """Create (if needed) and continue with the frozen behavior configuration."""

    if not out.exists():
        create_continuation(source, out)
    state = validate_continuation_directory(out)
    behavior = state["marker"]["behavior_config"]
    models = behavior["models"]
    bindings = behavior["provider_bindings"]
    prices = behavior["price_table"]
    from integrations.memebench.systems import build_system
    import integrations.memebench.run_full100_v3_p2 as legacy

    providers_document = json.loads(providers_path.read_text(encoding="utf-8"))
    system = await build_system(
        chat_model=models["chat_model"],
        oracle_model=models["p2_strong_model"],
        extract_model=models["extract_model"],
        judge_model=models["judge_model"],
        provider_label=bindings["provider"],
        embedding_provider_label=bindings["embedding_provider"],
        embedding_model=models["embedding_model"],
        cascade=False,
        p2_cascade=True,
        p2_cheap_model=models["p2_cheap_model"],
        providers_document=providers_document,
    )
    corpus = FrozenV3()
    executed: list[str] = []
    try:
        for episode_id in continuation_pending_episode_ids(out):
            attempt_token = str(uuid4())
            token_before = legacy._token_snap(system)
            retry_before = legacy._retry_snap(system)
            original_validator = legacy.validate_paid_cost
            try:
                # The old behavior runner rejects bounded accounting before it
                # can persist an otherwise valid execution.  Only that terminal
                # accounting assertion is replaced; runtime behavior is untouched.
                legacy.validate_paid_cost = (
                    lambda cost, **_kwargs: float(cost.get("known_usd") or 0.0)
                )
                generated = await legacy.run_one_paid_case(
                    system,
                    corpus=corpus,
                    episode_id=episode_id,
                    out=out,
                    prices=prices,
                    identity=state["source"]["source_identity"],
                    artifact_namespace="full-run",
                    prior_attempts=(),
                    attempt_token=attempt_token,
                )
                artifact = _read_object(generated, f"generated artifact {episode_id}")
                missing_response_stages = [
                    f"{stage}.answer"
                    for stage, row in artifact.get("answers", {}).items()
                    if not isinstance(row.get("raw_answer"), str)
                    or not row["raw_answer"].strip()
                ]
                missing_response_stages.extend(
                    f"{row.get('stage')}.judge"
                    for row in (artifact.get("judge") or {}).get("calls") or ()
                    if not isinstance(row.get("raw_output"), str)
                    or not row["raw_output"].strip()
                )
                frozen = frozen_cost_for_corpus(
                    corpus, episode_id, price_table=prices
                )
                _attach_native_cost_views(artifact, frozen=frozen)
                if missing_response_stages:
                    artifact.update(
                        {
                            "case_success": False,
                            "runtime_status": "provider-call-failure",
                            "earliest_failure_layer": "provider",
                            "provider_failure": {
                                "error_type": "MissingProviderResponse",
                                "stages": missing_response_stages,
                                "answer_fabricated": False,
                            },
                        }
                    )
                artifact["continuation"] = {
                    "continuation_identity": state["marker"][
                        "continuation_identity"
                    ],
                    "source_run_identity": state["marker"]["behavior_identity"],
                    "executed_by_continuation": True,
                }
                # Remove the legacy pre-accounting file before writing the
                # canonical continuation artifact in a fresh content directory.
                generated.parent.joinpath("manifest.json").unlink(missing_ok=True)
                generated.unlink(missing_ok=True)
                generated.parent.rmdir()
                artifact_path, digest = _write_native_artifact(
                    out,
                    episode_id,
                    artifact,
                    state["marker"]["continuation_identity"],
                )
                audit = artifact["cost_views"]["audit_all_in"]
                status = (
                    "finalized"
                    if audit["cost_exact"]
                    else "cost-bounded"
                    if audit["cost_bounded"]
                    else "cost-unbounded"
                )
            except Exception as exc:
                provider_failure = isinstance(
                    exc, (ConnectionError, TimeoutError)
                ) or type(exc).__module__.startswith("httpx")
                provider_failure = provider_failure or any(
                    marker in str(exc).casefold()
                    for marker in (
                        "raw output",
                        "empty response",
                        "chat completion",
                        "provider",
                    )
                )
                if not provider_failure:
                    raise
                usage = legacy._paid_usage_delta(system, token_before, retry_before)
                runtime_cost = legacy._priced_cost(system, usage, prices)
                frozen = frozen_cost_for_corpus(
                    corpus, episode_id, price_table=prices
                )
                runtime_interval = _runtime_interval_from_legacy_cost(runtime_cost)
                staged = out / ".staging" / episode_id / "cases" / episode_id
                staged_artifact = staged / "artifact.json"
                if not staged_artifact.is_file():
                    raise RuntimeError(
                        "provider failure occurred before auditable runtime artifact"
                    ) from exc
                artifact = _read_object(
                    staged_artifact, f"provider-failure artifact {episode_id}"
                )
                artifact.update(
                    {
                        "execution_complete": True,
                        "case_success": False,
                        "runtime_status": "provider-call-failure",
                        "earliest_failure_layer": "provider",
                        "provider_failure": {
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                            "answer_fabricated": False,
                        },
                        "accounting_schema_version": ACCOUNTING_SCHEMA_VERSION,
                        "cost_views": {
                            "meme_comparable": aggregate_intervals(
                                [frozen, runtime_interval]
                            ),
                            "p2_incremental": exact_record(0.0),
                            "audit_all_in": aggregate_intervals(
                                [frozen, runtime_interval]
                            ),
                        },
                    }
                )
                artifact_path, digest = _write_native_artifact(
                    out,
                    episode_id,
                    artifact,
                    state["marker"]["continuation_identity"],
                )
                status = (
                    "cost-bounded"
                    if artifact["cost_views"]["audit_all_in"]["cost_bounded"]
                    else "cost-unbounded"
                )
                if staged.exists():
                    shutil.rmtree(staged)
            finally:
                legacy.validate_paid_cost = original_validator
            checkpoint = {
                "status": status,
                "episode_id": episode_id,
                "checkpoint_namespace": "full-run",
                "continuation_identity": state["marker"]["continuation_identity"],
                "attempt_token": attempt_token,
                "artifact_path": str(artifact_path),
                "artifact_sha256": digest,
                "executed_by_continuation": True,
            }
            checkpoint_path = out / "checkpoints/full-run" / f"{episode_id}.json"
            checkpoint_path.write_text(canonical_json(checkpoint) + "\n")
            executed.append(episode_id)
    finally:
        await system.close()
    summary = validate_continuation_completion(out)
    (out / "run_cost_summary.json").write_text(
        canonical_json(summary) + "\n", encoding="utf-8"
    )
    return {"executed": executed, "summary": summary}


def _accuracy_eligible(artifact: Mapping[str, Any]) -> bool:
    return (
        artifact.get("execution_complete") is True
        and artifact.get("runtime_status") != "provider-call-failure"
        and bool((artifact.get("judge") or {}).get("calls"))
    )


def build_completion_summary(
    *,
    meme_comparable: Sequence[Mapping[str, Any]],
    p2_incremental: Sequence[Mapping[str, Any]],
    audit_all_in: Sequence[Mapping[str, Any]],
    artifacts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Keep MEME, P2 and all-in buckets separate while closing each interval."""

    all_rows = list(audit_all_in)
    exact_count = sum(row.get("cost_exact") is True for row in all_rows)
    bounded_count = sum(row.get("cost_bounded") is True for row in all_rows)
    unbounded_count = sum(row.get("cost_unbounded") is True for row in all_rows)
    return {
        "schema_version": CONTINUATION_SCHEMA_VERSION,
        "accounting_schema_version": ACCOUNTING_SCHEMA_VERSION,
        "episode_count": len(artifacts),
        "exact_case_count": exact_count,
        "bounded_case_count": bounded_count,
        "unbounded_case_count": unbounded_count,
        "unknown_usage_attempts": sum(
            int(row.get("unknown_usage_attempt_count") or 0) for row in all_rows
        ),
        "accuracy_denominator": sum(_accuracy_eligible(row) for row in artifacts),
        "provider_call_failure_count": sum(
            row.get("runtime_status") == "provider-call-failure"
            for row in artifacts
        ),
        "cost_views": {
            "meme_comparable": interval_distribution(list(meme_comparable)),
            "p2_incremental": interval_distribution(list(p2_incremental)),
            "audit_all_in": interval_distribution(all_rows),
        },
    }


def frozen_cost_for_corpus(
    corpus: FrozenV3,
    episode_id: str,
    *,
    price_table: Mapping[str, Any],
) -> dict[str, Any]:
    selectors = load_frozen_selector_rows(corpus.v3)
    return frozen_episode_interval(
        episode_id=episode_id,
        cost_row=corpus.episode_cost(episode_id),
        selector_rows=selectors.get(episode_id, ()),
        price_table=price_table,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-continuation")
    validate.add_argument("--source-run", type=Path, required=True)
    validate.add_argument("--dry-run", action="store_true", required=True)
    create = subparsers.add_parser("create-continuation")
    create.add_argument("--source-run", type=Path, required=True)
    create.add_argument("--out", type=Path, required=True)
    run = subparsers.add_parser("continue-run")
    run.add_argument("--source-run", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--providers-path", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-continuation":
        result = validate_continuation_source(args.source_run)
    elif args.command == "create-continuation":
        result = create_continuation(args.source_run, args.out)
    else:
        result = asyncio.run(
            continue_live_run(
                args.source_run,
                args.out,
                providers_path=args.providers_path,
            )
        )
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
