"""Deterministic, whole-episode, domain-stratified forward locking."""

from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
from typing import Any, Iterable, Mapping

from .schemas import SCHEMA_VERSION, canonical_sha256, opaque_id


SEED_COMMITMENT_PHRASE = "ContextHub PathA forward-lock v1"
SELECTION_ALGORITHM = "sha256(seed_uint64_be + NUL + raw_episode_id), ascending"


class ForwardLockError(ValueError):
    pass


def derive_committed_seed(
    dataset_sha256: str,
    *,
    phrase: str = SEED_COMMITMENT_PHRASE,
) -> int:
    if len(dataset_sha256) != 64 or any(c not in "0123456789abcdef" for c in dataset_sha256):
        raise ForwardLockError("dataset_sha256 must be 64 lowercase hex characters")
    digest = sha256(f"{phrase}\n{dataset_sha256}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _rank(seed: int, raw_episode_id: str) -> str:
    if not 0 <= seed < 2**64:
        raise ForwardLockError("seed must be an unsigned 64-bit integer")
    return sha256(seed.to_bytes(8, "big") + b"\0" + raw_episode_id.encode("utf-8")).hexdigest()


def make_forward_lock_manifest(
    episodes: Iterable[Mapping[str, Any]],
    *,
    dataset_sha256: str,
    seed: int | None,
    per_domain: int = 10,
    seed_method: str = "user_confirmed_uint64",
) -> dict[str, Any]:
    """Select exact stratum sizes without consulting any task or result field."""

    if seed is None:
        raise ForwardLockError("forward-lock seed is not confirmed")
    if per_domain <= 0:
        raise ForwardLockError("per_domain must be positive")
    episode_rows = list(episodes)
    if not episode_rows:
        raise ForwardLockError("cannot lock an empty dataset")
    raw_ids = [str(row.get("episode_id", "")) for row in episode_rows]
    if not all(raw_ids) or len(raw_ids) != len(set(raw_ids)):
        raise ForwardLockError("episode_id must be present and unique")
    strata: dict[str, list[str]] = defaultdict(list)
    for row in episode_rows:
        domain = row.get("domain")
        if not isinstance(domain, str) or not domain:
            raise ForwardLockError("every episode must have a non-empty domain")
        strata[domain].append(str(row["episode_id"]))
    if len(strata) != 2 or Counter(map(len, strata.values())) != Counter({50: 2}):
        raise ForwardLockError(
            f"expected exactly two 50-episode domains, got "
            f"{dict(sorted((key, len(value)) for key, value in strata.items()))}"
        )

    locked_raw: list[str] = []
    stratum_manifest: dict[str, Any] = {}
    for domain in sorted(strata):
        ranked = sorted(strata[domain], key=lambda episode_id: (_rank(seed, episode_id), episode_id))
        selected = ranked[:per_domain]
        development = ranked[per_domain:]
        locked_raw.extend(selected)
        stratum_manifest[domain] = {
            "episode_count": len(ranked),
            "locked_count": len(selected),
            "development_count": len(development),
            "locked_raw_episode_ids": selected,
            "development_raw_episode_ids": development,
        }

    locked_set = set(locked_raw)
    ordered_by_source = raw_ids
    locked_entries = [
        {
            "raw_episode_id": raw_id,
            "episode_id": opaque_id("episode", dataset_sha256, raw_id),
            "domain": next(str(row["domain"]) for row in episode_rows if row["episode_id"] == raw_id),
            "selection_rank_sha256": _rank(seed, raw_id),
        }
        for raw_id in ordered_by_source
        if raw_id in locked_set
    ]
    development_entries = [
        {
            "raw_episode_id": raw_id,
            "episode_id": opaque_id("episode", dataset_sha256, raw_id),
            "domain": next(str(row["domain"]) for row in episode_rows if row["episode_id"] == raw_id),
            "selection_rank_sha256": _rank(seed, raw_id),
        }
        for raw_id in ordered_by_source
        if raw_id not in locked_set
    ]
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "manifest_version": "forward-lock-v1",
        "status": "locked",
        "dataset": {
            "variant": "filler32k",
            "sha256": dataset_sha256,
            "episode_count": len(episode_rows),
        },
        "selection": {
            "unit": "complete_episode",
            "stratification_field": "domain",
            "per_domain": per_domain,
            "seed_uint64": seed,
            "seed_method": seed_method,
            "seed_commitment_phrase": (
                SEED_COMMITMENT_PHRASE if seed_method == "sha256_commitment" else None
            ),
            "algorithm": SELECTION_ALGORITHM,
            "prohibited_inputs": ["system_output", "system_score", "experiment_result"],
        },
        "strata": stratum_manifest,
        "forward_locked_episodes": locked_entries,
        "development_episodes": development_entries,
    }
    payload["manifest_sha256"] = canonical_sha256(payload)
    validate_forward_lock_manifest(payload)
    return payload


def validate_forward_lock_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("status") != "locked":
        raise ForwardLockError("forward-lock manifest is not locked")
    expected_hash = manifest.get("manifest_sha256")
    payload = dict(manifest)
    payload.pop("manifest_sha256", None)
    if canonical_sha256(payload) != expected_hash:
        raise ForwardLockError("forward-lock manifest hash mismatch")
    locked = list(manifest.get("forward_locked_episodes") or [])
    development = list(manifest.get("development_episodes") or [])
    if len(locked) != 20 or len(development) != 80:
        raise ForwardLockError("forward-lock must contain exactly 20 locked and 80 development episodes")
    all_ids = [row.get("episode_id") for row in locked + development]
    raw_ids = [row.get("raw_episode_id") for row in locked + development]
    if len(set(all_ids)) != 100 or len(set(raw_ids)) != 100:
        raise ForwardLockError("forward-lock partitions overlap or omit episodes")
    domain_counts = Counter(row.get("domain") for row in locked)
    if domain_counts != Counter({"personal_life": 10, "software_project": 10}):
        raise ForwardLockError(f"locked domain distribution is not 10/10: {dict(domain_counts)}")
