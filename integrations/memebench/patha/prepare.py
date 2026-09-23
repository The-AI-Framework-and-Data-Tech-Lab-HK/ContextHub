"""Prepare the real S1 MEME inputs, answer bundles, labels, and manifests."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .firewall import ALLOWED_MAINTENANCE_JSON_POINTERS, FORBIDDEN_RUNTIME_FIELDS
from .forward_lock import derive_committed_seed, make_forward_lock_manifest
from .schemas import (
    AnswerBundle,
    MaintenanceInput,
    SCHEMA_VERSION,
    ScoringSidecar,
    canonical_json_bytes,
    canonical_sha256,
    opaque_id,
)
from .scoring_contract import build_scoring_contract


DEFAULT_DATA_PATH = Path(
    "/Users/sherrylin/Documents/PythonProjects/public/MEME/meme_filler32k.json"
)
EXPECTED_DATA_SHA256 = "a88d28374a002b3e5b1683fb7201d06a1ce739d2ebf94c971c37bb65cf6ebdd3"
HF_DATASET_REVISION = "03932fd33a08debf182ad01a47504024201d86f2"
MEME_PUBLIC_REVISION = "0271ad85389a963cbc4892a36391f868ba4d18d1"
MEME_PUBLIC_ROOT = Path("/Users/sherrylin/Documents/PythonProjects/public/MEME-public")

MAINTENANCE_REL = Path("inputs/maintenance-inputs-v1.jsonl")
ANSWERS_REL = Path("answers/answer-bundles-v1.jsonl")
SCOREABLE_REL = Path("labels/scoreable-records-v1.jsonl")
PATHS_REL = Path("labels/necessary-paths-v1.jsonl")
FORWARD_LOCK_REL = Path("manifests/forward-lock-v1.json")
ALLOWED_INPUTS_REL = Path("manifests/allowed-inputs-v1.json")
SCORER_CONTRACT_REL = Path("manifests/scorer-contract-v1.json")

R3_SCOREABLE_REL = Path("labels/scoreable-records-v2.jsonl")
R3_PATHS_REL = Path("labels/necessary-paths-v2.jsonl")
R3_ALLOWED_INPUTS_REL = Path("manifests/allowed-inputs-v2.json")
R3_SCORER_CONTRACT_REL = Path("manifests/scorer-contract-v2.json")
DEFAULT_MATERIAL_VERSION = "v2"
SUPPORTED_MATERIAL_VERSIONS = ("v1", "v2")

R3_SUPPLEMENTED_EPISODES = frozenset(
    {
        "sw_001",
        "sw_003",
        "sw_005",
        "sw_015",
        "sw_016",
        "sw_019",
        "sw_020",
        "sw_026",
        "sw_037",
        "sw_039",
        "sw_050",
    }
)
R3_INCLUDED_ANOMALY = ("pl_030", 4, "commute_method")


class PreparationError(ValueError):
    pass


def artifact_layout(material_version: str) -> dict[str, Path]:
    if material_version not in SUPPORTED_MATERIAL_VERSIONS:
        raise PreparationError(f"unsupported S1 material version: {material_version}")
    if material_version == "v1":
        return {
            "maintenance": MAINTENANCE_REL,
            "answers": ANSWERS_REL,
            "scoreable": SCOREABLE_REL,
            "paths": PATHS_REL,
            "forward_lock": FORWARD_LOCK_REL,
            "allowed_inputs": ALLOWED_INPUTS_REL,
            "scorer_contract": SCORER_CONTRACT_REL,
        }
    return {
        # Input, question, and split bytes did not change in S1-R3.  The v2
        # manifests intentionally reference those immutable v1 artifacts.
        "maintenance": MAINTENANCE_REL,
        "answers": ANSWERS_REL,
        "scoreable": R3_SCOREABLE_REL,
        "paths": R3_PATHS_REL,
        "forward_lock": FORWARD_LOCK_REL,
        "allowed_inputs": R3_ALLOWED_INPUTS_REL,
        "scorer_contract": R3_SCORER_CONTRACT_REL,
    }


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_line(value: Mapping[str, Any]) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _write_immutable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == content:
            return
        raise PreparationError(f"refusing to overwrite changed frozen artifact: {path}")
    path.write_bytes(content)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _write_immutable(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    _write_immutable(path, b"".join(_canonical_line(row) for row in rows))


def load_and_validate_dataset(path: Path, expected_sha256: str = EXPECTED_DATA_SHA256) -> tuple[list[dict[str, Any]], str]:
    actual_hash = sha256_file(path)
    if actual_hash != expected_sha256:
        raise PreparationError(
            f"dataset hash mismatch: expected {expected_sha256}, got {actual_hash}"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or len(data) != 100:
        raise PreparationError("default filler32k dataset must contain exactly 100 episodes")
    raw_ids = [episode.get("episode_id") for episode in data]
    if len(set(raw_ids)) != 100 or any(not value for value in raw_ids):
        raise PreparationError("dataset episode IDs must be present and unique")
    domains = Counter(episode.get("domain") for episode in data)
    if domains != Counter({"personal_life": 50, "software_project": 50}):
        raise PreparationError(f"unexpected domain distribution: {dict(domains)}")
    for episode in data:
        sessions = episode.get("sessions")
        if not isinstance(sessions, list) or len(sessions) != episode.get("total_sessions"):
            raise PreparationError(f"incomplete session list in {episode.get('episode_id')}")
        for phase in ("before", "after"):
            package = episode.get(f"{phase}_questions") or {}
            position = package.get("position_after_session")
            if not isinstance(position, int) or not 0 <= position < len(sessions):
                raise PreparationError(f"invalid {phase} question boundary in {episode['episode_id']}")
    return data, actual_hash


def _messages(raw_session: Mapping[str, Any]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for raw in raw_session.get("conversation") or []:
        role = raw.get("role")
        content = raw.get("content")
        if role not in {"user", "assistant", "system", "tool"} or not isinstance(content, str):
            raise PreparationError("session contains an invalid conversation message")
        messages.append({"role": role, "content": content})
    if not messages:
        raise PreparationError("session conversation cannot be empty")
    return messages


def build_maintenance_inputs(
    episodes: list[dict[str, Any]], dataset_sha256: str
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    dataset_id = f"meme_filler32k@{dataset_sha256[:12]}"
    rows: list[dict[str, Any]] = []
    by_raw_id: dict[str, dict[str, Any]] = {}
    for episode in episodes:
        raw_episode_id = episode["episode_id"]
        runtime_episode_id = opaque_id("episode", dataset_sha256, raw_episode_id)
        sources: list[dict[str, Any]] = []
        for ordinal, session in enumerate(episode["sessions"]):
            messages = _messages(session)
            sources.append(
                {
                    "source_id": opaque_id(
                        "source", dataset_sha256, raw_episode_id, ordinal, session.get("session_id", "")
                    ),
                    "ordinal": ordinal,
                    "source_version": 1,
                    "timestamp": session.get("timestamp"),
                    "messages": messages,
                    "content_sha256": canonical_sha256(messages),
                }
            )
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "dataset_id": dataset_id,
            "episode_id": runtime_episode_id,
            "sources": sources,
        }
        payload["input_sha256"] = canonical_sha256(payload)
        validated = MaintenanceInput.model_validate(payload).model_dump(mode="json")
        rows.append(validated)
        by_raw_id[raw_episode_id] = validated
    return rows, by_raw_id


def _prefix_hash(sources: list[dict[str, Any]], end_ordinal: int) -> str:
    identity = [
        {
            "source_id": source["source_id"],
            "source_version": source["source_version"],
            "content_sha256": source["content_sha256"],
        }
        for source in sources[: end_ordinal + 1]
    ]
    return canonical_sha256(identity)


def build_answer_bundles(
    episodes: list[dict[str, Any]],
    dataset_sha256: str,
    maintenance_by_raw_id: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[tuple[str, str, int], str]]:
    bundles: list[dict[str, Any]] = []
    question_ids: dict[tuple[str, str, int], str] = {}
    for episode in episodes:
        raw_episode_id = episode["episode_id"]
        maintenance = maintenance_by_raw_id[raw_episode_id]
        sources = list(maintenance["sources"])
        for phase in ("before", "after"):
            raw_bundle = episode[f"{phase}_questions"]
            release_ordinal = int(raw_bundle["position_after_session"])
            questions: list[dict[str, str]] = []
            for index, raw_question in enumerate(raw_bundle["questions"]):
                question_id = opaque_id(
                    "question", dataset_sha256, raw_episode_id, phase, index
                )
                question_ids[(raw_episode_id, phase, index)] = question_id
                questions.append({"question_id": question_id, "text": raw_question["question"]})
            payload: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "episode_id": maintenance["episode_id"],
                "bundle_id": opaque_id("bundle", dataset_sha256, raw_episode_id, phase),
                "phase": phase,
                "release_after_ordinal": release_ordinal,
                "release_after_source_id": sources[release_ordinal]["source_id"],
                "source_prefix_sha256": _prefix_hash(sources, release_ordinal),
                "sealed_state_required": True,
                "questions": questions,
            }
            payload["bundle_sha256"] = canonical_sha256(payload)
            bundles.append(AnswerBundle.model_validate(payload).model_dump(mode="json"))
    return bundles, question_ids


def _question_match_index(episode: Mapping[str, Any], phase: str, task: Mapping[str, Any]) -> tuple[int | None, str | None]:
    matches: list[int] = []
    for index, question in enumerate(episode[f"{phase}_questions"]["questions"]):
        if (
            question.get("task_type") == task.get("type")
            and question.get("entity") == task.get("target_entities")
            and question.get("hop") == task.get("hop")
            and question.get("question") == task.get("question_template")
        ):
            matches.append(index)
    if len(matches) != 1:
        return None, f"question_{phase}_match_count:{len(matches)}"
    return matches[0], None


def _all_simple_paths(adjacency: Mapping[str, set[str]], source: str, target: str) -> list[list[str]]:
    paths: list[list[str]] = []

    def visit(node: str, path: list[str]) -> None:
        if node == target:
            paths.append(path)
            return
        if len(path) > 8:
            return
        for child in sorted(adjacency.get(node, set())):
            if child not in path:
                visit(child, [*path, child])

    visit(source, [source])
    return paths


def _edge_evidence(
    episode: Mapping[str, Any],
    maintenance: Mapping[str, Any],
    source_entity: str,
    target_entity: str,
    *,
    through_ordinal: int | None = None,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for ordinal, session in enumerate(episode["sessions"]):
        if through_ordinal is not None and ordinal > through_ordinal:
            break
        for fact in session.get("gold_facts") or []:
            if not (
                fact.get("entity") == target_entity
                and fact.get("dependency_source") == source_entity
                and fact.get("has_dependency") is True
            ):
                continue
            turn_index = fact.get("conveyed_in_turn")
            excerpt = None
            role = None
            if isinstance(turn_index, int) and 0 <= turn_index < len(session["conversation"]):
                message = session["conversation"][turn_index]
                excerpt = message.get("content")
                role = message.get("role")
            evidence.append(
                {
                    "source_id": maintenance["sources"][ordinal]["source_id"],
                    "source_ordinal": ordinal,
                    "turn_index": turn_index,
                    "role": role,
                    "excerpt": excerpt,
                    "excerpt_sha256": sha256((excerpt or "").encode("utf-8")).hexdigest(),
                    "gold_fact_sha256": canonical_sha256(fact),
                    "basis": "raw gold_facts dependency metadata cross-checked to dialogue turn",
                }
            )
    return evidence


def _dialogue_dependency_pairs(
    episode: Mapping[str, Any], *, through_ordinal: int
) -> set[tuple[str, str]]:
    """Return explicit dependency pairs present by the before-question seal."""

    pairs: set[tuple[str, str]] = set()
    for session in episode["sessions"][: through_ordinal + 1]:
        for fact in session.get("gold_facts") or []:
            source = fact.get("dependency_source")
            target = fact.get("entity")
            if (
                fact.get("has_dependency") is True
                and isinstance(source, str)
                and source
                and isinstance(target, str)
                and target
            ):
                pairs.add((source, target))
    return pairs


def _root_change_evidence(
    episode: Mapping[str, Any], maintenance: Mapping[str, Any]
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for ordinal, session in enumerate(episode["sessions"]):
        for fact in session.get("gold_facts") or []:
            if fact.get("type") != "root_change" or fact.get("entity") != episode.get("root"):
                continue
            turn_index = fact.get("conveyed_in_turn")
            excerpt = None
            if isinstance(turn_index, int) and 0 <= turn_index < len(session["conversation"]):
                excerpt = session["conversation"][turn_index].get("content")
            evidence.append(
                {
                    "source_id": maintenance["sources"][ordinal]["source_id"],
                    "source_ordinal": ordinal,
                    "turn_index": turn_index,
                    "excerpt": excerpt,
                    "excerpt_sha256": sha256((excerpt or "").encode("utf-8")).hexdigest(),
                    "gold_fact_sha256": canonical_sha256(fact),
                    "basis": "raw gold_facts root_change metadata cross-checked to dialogue turn",
                }
            )
    return evidence


def build_necessary_paths(
    episodes: list[dict[str, Any]],
    dataset_sha256: str,
    maintenance_by_raw_id: Mapping[str, Mapping[str, Any]],
    *,
    material_version: str = "v1",
) -> tuple[list[dict[str, Any]], dict[tuple[str, int], dict[str, Any]]]:
    if material_version not in SUPPORTED_MATERIAL_VERSIONS:
        raise PreparationError(f"unsupported S1 material version: {material_version}")
    rows: list[dict[str, Any]] = []
    by_task: dict[tuple[str, int], dict[str, Any]] = {}
    supplemented_episodes: set[str] = set()
    for episode in episodes:
        raw_episode_id = episode["episode_id"]
        maintenance = maintenance_by_raw_id[raw_episode_id]
        raw_edges = list(episode.get("dependency_edges_used") or [])
        pair_indices: dict[tuple[str, str], list[int]] = defaultdict(list)
        raw_adjacency: dict[str, set[str]] = defaultdict(set)
        for edge_index, edge in enumerate(raw_edges):
            pair = (edge["source"], edge["target"])
            pair_indices[pair].append(edge_index)
            raw_adjacency[pair[0]].add(pair[1])
        before_ordinal = int(episode["before_questions"]["position_after_session"])
        dialogue_pairs = _dialogue_dependency_pairs(
            episode, through_ordinal=before_ordinal
        )
        adjacency: dict[str, set[str]] = defaultdict(set)
        for source, targets in raw_adjacency.items():
            adjacency[source].update(targets)
        if material_version == "v2":
            for source, target in dialogue_pairs:
                adjacency[source].add(target)
        for task_index, task in enumerate(episode.get("tasks") or []):
            if task.get("type") not in {"Cas", "Abs"}:
                continue
            record_id = opaque_id("record", dataset_sha256, raw_episode_id, task_index)
            target = task["target_entities"][0]
            candidates = _all_simple_paths(adjacency, episode["root"], target)
            raw_candidates = _all_simple_paths(
                raw_adjacency, episode["root"], target
            )
            issues: list[str] = []
            observations: list[str] = []
            data_quality_notes: list[str] = []
            if material_version == "v2" and candidates != raw_candidates:
                supplemented_episodes.add(raw_episode_id)
                data_quality_notes.append(
                    "dependency_edges_used_incomplete:path_supplemented_from_"
                    "before_boundary_gold_facts_and_dialogue"
                )
            if len(candidates) != 1:
                issues.append(f"complete_root_to_target_path_count:{len(candidates)}")
            elif len(candidates[0]) - 1 != task.get("hop"):
                issues.append(
                    f"path_hop_mismatch:label={task.get('hop')},path={len(candidates[0]) - 1}"
                )
            candidate_rows: list[dict[str, Any]] = []
            for path_index, path in enumerate(candidates):
                edge_rows: list[dict[str, Any]] = []
                for source_entity, target_entity in zip(path, path[1:]):
                    indices = pair_indices[(source_entity, target_entity)]
                    if material_version == "v2" and not indices:
                        data_quality_notes.append(
                            "dependency_edges_used_missing_edge:"
                            f"{source_entity}->{target_entity}"
                        )
                    if len(indices) > 1:
                        observations.append(
                            f"duplicate_dataset_edge_declaration:{source_entity}->{target_entity}:" +
                            ",".join(str(value) for value in indices)
                        )
                    evidence = _edge_evidence(
                        episode,
                        maintenance,
                        source_entity,
                        target_entity,
                        through_ordinal=(
                            before_ordinal if material_version == "v2" else None
                        ),
                    )
                    if not evidence:
                        missing_note = (
                            f"missing_dialogue_edge_evidence:"
                            f"{source_entity}->{target_entity}"
                        )
                        if (
                            material_version == "v2"
                            and (raw_episode_id, task_index, target)
                            == R3_INCLUDED_ANOMALY
                        ):
                            data_quality_notes.append(missing_note)
                        else:
                            issues.append(missing_note)
                    declarations = [raw_edges[index] for index in indices]
                    edge_row: dict[str, Any] = {
                        "source_entity": source_entity,
                        "target_entity": target_entity,
                        "dataset_edge_indices": indices,
                        "dataset_edge_declarations": declarations,
                        "dialogue_evidence": evidence,
                    }
                    if material_version == "v2":
                        edge_row["evidence_sources"] = {
                            "dependency_edges_used": bool(indices),
                            "before_boundary_gold_facts_and_dialogue": bool(evidence),
                        }
                    edge_rows.append(edge_row)
                candidate_rows.append(
                    {
                        "path_id": f"path_{path_index}",
                        "entities": path,
                        "edges": edge_rows,
                        "direction": "event_root_to_target",
                        "p2_traversable_if_all_edges_delivered": True,
                    }
                )
            change_evidence = _root_change_evidence(episode, maintenance)
            if len(change_evidence) != 1:
                issues.append(f"root_change_evidence_count:{len(change_evidence)}")
            if (
                material_version == "v2"
                and (raw_episode_id, task_index, target) == R3_INCLUDED_ANOMALY
            ):
                data_quality_notes.extend(
                    [
                        "original_dialogue_conflict:remote_no_commute_vs_10_min_commute",
                        "user_decision_2026-09-23:retain_benchmark_label_and_include_"
                        "record_in_mechanism_and_official_scoring",
                    ]
                )
            row: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "record_id": record_id,
                "episode_id": maintenance["episode_id"],
                "raw_episode_id": raw_episode_id,
                "task_index": task_index,
                "event_root": episode["root"],
                "target_entity": target,
                "declared_hop": task.get("hop"),
                "path_status": "scoreable" if not issues else "needs_adjudication",
                "basis": [
                    "raw dependency_edges_used",
                    "raw Cas/Abs task target and hop",
                    "raw dialogue turns referenced by gold_facts",
                ],
                "issues": sorted(set(issues)),
                "observations": sorted(set(observations)),
                "root_change_evidence": change_evidence,
                "necessary_path_candidates": candidate_rows,
            }
            if material_version == "v2":
                row.update(
                    {
                        "material_version": "necessary-paths-v2",
                        "adjudication_status": "resolved_included",
                        "mechanism_scoring_included": True,
                        "official_answer_scoring_included": True,
                        "evidence_status": (
                            "benchmark_label_with_data_quality_note"
                            if (raw_episode_id, task_index, target)
                            == R3_INCLUDED_ANOMALY
                            else "dialogue_supported"
                        ),
                        "data_quality_notes": sorted(set(data_quality_notes)),
                    }
                )
            row["record_sha256"] = canonical_sha256(row)
            rows.append(row)
            by_task[(raw_episode_id, task_index)] = row
    if material_version == "v2" and supplemented_episodes != R3_SUPPLEMENTED_EPISODES:
        raise PreparationError(
            "S1-R3 supplemented episode set differs from the reviewed 11 records: "
            f"{sorted(supplemented_episodes)}"
        )
    return rows, by_task


def build_scoring_sidecars(
    episodes: list[dict[str, Any]],
    dataset_sha256: str,
    maintenance_by_raw_id: Mapping[str, Mapping[str, Any]],
    question_ids: Mapping[tuple[str, str, int], str],
    path_by_task: Mapping[tuple[str, int], Mapping[str, Any]],
    *,
    material_version: str = "v1",
) -> list[dict[str, Any]]:
    if material_version not in SUPPORTED_MATERIAL_VERSIONS:
        raise PreparationError(f"unsupported S1 material version: {material_version}")
    rows: list[dict[str, Any]] = []
    for episode in episodes:
        raw_episode_id = episode["episode_id"]
        for task_index, task in enumerate(episode.get("tasks") or []):
            if task.get("type") not in {"Cas", "Abs"}:
                continue
            issues: list[str] = []
            before_index, before_issue = _question_match_index(episode, "before", task)
            after_index, after_issue = _question_match_index(episode, "after", task)
            if before_issue:
                issues.append(before_issue)
            if after_issue:
                issues.append(after_issue)
            path_row = path_by_task[(raw_episode_id, task_index)]
            issues.extend(path_row["issues"])
            if before_index is None or after_index is None:
                # Keep the row and give it deterministic placeholder IDs.  The
                # status prevents a scorer from silently treating it as valid.
                before_id = opaque_id("question", dataset_sha256, raw_episode_id, "missing-before", task_index)
                after_id = opaque_id("question", dataset_sha256, raw_episode_id, "missing-after", task_index)
                before_question: Mapping[str, Any] = {}
                after_question: Mapping[str, Any] = {}
            else:
                before_id = question_ids[(raw_episode_id, "before", before_index)]
                after_id = question_ids[(raw_episode_id, "after", after_index)]
                before_question = episode["before_questions"]["questions"][before_index]
                after_question = episode["after_questions"]["questions"][after_index]
            payload: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "record_id": path_row["record_id"],
                "episode_id": maintenance_by_raw_id[raw_episode_id]["episode_id"],
                "raw_episode_id": raw_episode_id,
                "domain": episode["domain"],
                "task_type": task["type"],
                "hop": task["hop"],
                "target_entities": task["target_entities"],
                "event_root": episode["root"],
                "event_before": episode["root_change"]["before"],
                "event_after": episode["root_change"]["after"],
                "before_question_id": before_id,
                "after_question_id": after_id,
                "before_entity_values": dict(before_question.get("entity_values") or {}),
                "after_entity_values": dict(after_question.get("entity_values") or {}),
                "before_expected_answer": str(before_question.get("expected_answer") or ""),
                "after_gold_answer": str(after_question.get("gold_answer") or ""),
                "answer_pair_status": (
                    "scoreable" if before_index is not None and after_index is not None else "needs_adjudication"
                ),
                "path_status": path_row["path_status"],
                "issues": sorted(set(issues)),
            }
            if material_version == "v2":
                payload.update(
                    {
                        "adjudication_status": "resolved_included",
                        "mechanism_scoring_included": True,
                        "official_answer_scoring_included": True,
                        "data_quality_notes": path_row["data_quality_notes"],
                    }
                )
            payload["record_sha256"] = canonical_sha256(payload)
            rows.append(
                ScoringSidecar.model_validate(payload).model_dump(
                    mode="json", exclude_none=True
                )
            )
    return rows


def _code_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parent
    names = [
        "schemas.py",
        "prepare.py",
        "forward_lock.py",
        "firewall.py",
        "preflight.py",
        "scoring_contract.py",
    ]
    return {name: sha256_file(root / name) for name in names}


def _self_hashed_manifest(payload: dict[str, Any], field: str = "manifest_sha256") -> dict[str, Any]:
    payload[field] = canonical_sha256(payload)
    return payload


def prepare_all(
    *,
    data_path: Path,
    output_root: Path,
    seed: int | None,
    seed_method: str,
    expected_dataset_sha256: str = EXPECTED_DATA_SHA256,
    material_version: str = DEFAULT_MATERIAL_VERSION,
) -> dict[str, Any]:
    layout = artifact_layout(material_version)
    episodes, dataset_sha256 = load_and_validate_dataset(data_path, expected_dataset_sha256)
    maintenance_rows, maintenance_by_raw_id = build_maintenance_inputs(episodes, dataset_sha256)
    answer_rows, question_ids = build_answer_bundles(
        episodes, dataset_sha256, maintenance_by_raw_id
    )
    path_rows, path_by_task = build_necessary_paths(
        episodes,
        dataset_sha256,
        maintenance_by_raw_id,
        material_version=material_version,
    )
    scoreable_rows = build_scoring_sidecars(
        episodes,
        dataset_sha256,
        maintenance_by_raw_id,
        question_ids,
        path_by_task,
        material_version=material_version,
    )
    forward_lock = make_forward_lock_manifest(
        episodes,
        dataset_sha256=dataset_sha256,
        seed=seed,
        seed_method=seed_method,
    )

    _write_jsonl(output_root / layout["maintenance"], maintenance_rows)
    _write_jsonl(output_root / layout["answers"], answer_rows)
    _write_jsonl(output_root / layout["scoreable"], scoreable_rows)
    _write_jsonl(output_root / layout["paths"], path_rows)
    _write_json(output_root / layout["forward_lock"], forward_lock)

    artifact_hashes = {
        str(path): sha256_file(output_root / path)
        for path in [
            layout["maintenance"],
            layout["answers"],
            layout["scoreable"],
            layout["paths"],
            layout["forward_lock"],
        ]
    }
    source_counts = Counter(
        session.get("type") for episode in episodes for session in episode["sessions"]
    )
    path_counts = Counter(row["path_status"] for row in path_rows)
    allowed_manifest = _self_hashed_manifest(
        {
            "schema_version": SCHEMA_VERSION,
            "manifest_version": f"allowed-inputs-{material_version}",
            "material_version": material_version,
            "dataset": {
                "repository": "meme-benchmark/MEME",
                "variant": "filler32k",
                "default_variant": True,
                "huggingface_revision": HF_DATASET_REVISION,
                "source_url": (
                    "https://huggingface.co/datasets/meme-benchmark/MEME/resolve/"
                    f"{HF_DATASET_REVISION}/meme_filler32k.json"
                ),
                "local_source_path": str(data_path),
                "sha256": dataset_sha256,
                "size_bytes": data_path.stat().st_size,
                "episode_count": len(episodes),
                "domain_distribution": dict(
                    sorted(Counter(episode["domain"] for episode in episodes).items())
                ),
                "session_count": sum(len(episode["sessions"]) for episode in episodes),
                "source_session_type_counts_offline_audit_only": dict(sorted(source_counts.items())),
                "reference_code_repository": "SeokwonJung-Jay/MEME-public",
                "reference_code_revision": MEME_PUBLIC_REVISION,
            },
            "maintenance_runtime": {
                "artifact": str(layout["maintenance"]),
                "artifact_sha256": artifact_hashes[str(layout["maintenance"])],
                "allowed_json_pointers": list(ALLOWED_MAINTENANCE_JSON_POINTERS),
                "forbidden_normalized_field_names": sorted(FORBIDDEN_RUNTIME_FIELDS),
                "id_policy": "opaque fixed-prefix plus 24 lowercase hexadecimal characters",
                "allowed_semantics": [
                    "opaque episode/source identity",
                    "chronological ordinal",
                    "source version",
                    "timestamp",
                    "raw conversation role and content",
                    "content hash",
                ],
            },
            "answer_runtime_after_state_seal": {
                "artifact": str(layout["answers"]),
                "artifact_sha256": artifact_hashes[str(layout["answers"])],
                "contains": ["opaque IDs", "release boundary", "question text"],
                "does_not_contain": ["task type", "hop", "gold", "entity_values", "path labels"],
                "sealed_state_required": True,
            },
            "offline_scoring_only": {
                "scoreable_records_artifact": str(layout["scoreable"]),
                "scoreable_records_sha256": artifact_hashes[str(layout["scoreable"])],
                "necessary_paths_artifact": str(layout["paths"]),
                "necessary_paths_sha256": artifact_hashes[str(layout["paths"])],
                "record_count": len(scoreable_rows),
                "necessary_path_status_counts": dict(sorted(path_counts.items())),
            },
            "forward_lock": {
                "artifact": str(layout["forward_lock"]),
                "artifact_sha256": artifact_hashes[str(layout["forward_lock"])],
                "manifest_sha256": forward_lock["manifest_sha256"],
            },
            "generator_code_sha256": _code_hashes(),
        }
    )
    _write_json(output_root / layout["allowed_inputs"], allowed_manifest)
    artifact_hashes[str(layout["allowed_inputs"])] = sha256_file(
        output_root / layout["allowed_inputs"]
    )

    code_hashes = _code_hashes()
    locked_episode_ids = {
        row["episode_id"] for row in forward_lock["forward_locked_episodes"]
    }
    locked_record_count = sum(
        row["episode_id"] in locked_episode_ids for row in scoreable_rows
    )
    contract = build_scoring_contract(
        scoreable_rows,
        path_rows,
        artifact_hashes=artifact_hashes,
        contract_validator_code={
            "scoring_contract.py": code_hashes["scoring_contract.py"],
            "preflight.py": code_hashes["preflight.py"],
        },
        official_reference={
            "repository": "SeokwonJung-Jay/MEME-public",
            "revision": MEME_PUBLIC_REVISION,
            "judge_path": "code/eval/judge.py",
            "judge_sha256": sha256_file(MEME_PUBLIC_ROOT / "code/eval/judge.py"),
            "runner_path": "code/eval/run_agent.py",
            "runner_sha256": sha256_file(MEME_PUBLIC_ROOT / "code/eval/run_agent.py"),
            "dataset_card_path": "dataset/README.md",
            "dataset_card_sha256": sha256_file(MEME_PUBLIC_ROOT / "dataset/README.md"),
            "adaptation_status": "pending_s8",
        },
        contract_version=f"scorer-contract-{material_version}",
        adjudication_decision=(
            {
                "decision_date": "2026-09-23",
                "decision_source": "S1-review.md §6.2 user decision",
                "mechanism_record_count": 294,
                "official_answer_record_count": 294,
                "cas_record_count": 164,
                "abs_record_count": 130,
                "forward_locked_record_count": locked_record_count,
                "development_record_count": len(scoreable_rows) - locked_record_count,
                "supplemented_dialogue_path_record_count": 11,
                "supplemented_raw_episode_ids": sorted(R3_SUPPLEMENTED_EPISODES),
                "pl_030": {
                    "task_index": 4,
                    "target_entity": "commute_method",
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
            if material_version == "v2"
            else None
        ),
    )
    _write_json(output_root / layout["scorer_contract"], contract)
    return {
        "material_version": material_version,
        "dataset_sha256": dataset_sha256,
        "episodes": len(episodes),
        "maintenance_inputs": len(maintenance_rows),
        "answer_bundles": len(answer_rows),
        "scoreable_records": len(scoreable_rows),
        "necessary_path_status_counts": dict(sorted(path_counts.items())),
        "forward_locked": len(forward_lock["forward_locked_episodes"]),
        "development": len(forward_lock["development_episodes"]),
        "output_root": str(output_root),
        "artifact_hashes": {
            **artifact_hashes,
            str(layout["scorer_contract"]): sha256_file(
                output_root / layout["scorer_contract"]
            ),
        },
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA_PATH)
    parser.add_argument("--output-root", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--seed", type=int)
    group.add_argument("--derive-seed", action="store_true")
    parser.add_argument(
        "--material-version",
        choices=SUPPORTED_MATERIAL_VERSIONS,
        default=DEFAULT_MATERIAL_VERSION,
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    data_hash = sha256_file(args.data)
    seed = derive_committed_seed(data_hash) if args.derive_seed else args.seed
    method = "sha256_commitment" if args.derive_seed else "user_confirmed_uint64"
    summary = prepare_all(
        data_path=args.data,
        output_root=args.output_root,
        seed=seed,
        seed_method=method,
        material_version=args.material_version,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
