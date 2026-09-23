"""Runtime firewall for MEME Path A maintenance inputs."""

from __future__ import annotations

import re
from typing import Any, Mapping

from .schemas import MaintenanceInput, canonical_sha256


def _normalise_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


# Includes raw-data structures that are not named "gold" but encode the answer
# graph or task.  This list is versioned in allowed-inputs-v1.json as well.
FORBIDDEN_RUNTIME_FIELDS = frozenset(
    {
        "domain",
        "type",
        "sessiontype",
        "sessionid",
        "evidencetype",
        "evidencesessionindices",
        "gold",
        "goldanswer",
        "goldfacts",
        "expectedanswer",
        "entityvalues",
        "question",
        "questions",
        "questiontemplate",
        "task",
        "tasks",
        "tasktype",
        "hop",
        "root",
        "rootchange",
        "chainentities",
        "fillerentities",
        "entities",
        "dependencyedgesused",
        "cascadesource",
        "necessarypath",
        "necessarypaths",
        "pathlabels",
        "scoreable",
        "score",
        "beforequestions",
        "afterquestions",
        "has2hop",
    }
)


ALLOWED_MAINTENANCE_JSON_POINTERS = (
    "/schema_version",
    "/dataset_id",
    "/episode_id",
    "/sources",
    "/sources/*/source_id",
    "/sources/*/ordinal",
    "/sources/*/source_version",
    "/sources/*/timestamp",
    "/sources/*/messages",
    "/sources/*/messages/*/role",
    "/sources/*/messages/*/content",
    "/sources/*/content_sha256",
    "/input_sha256",
)


class InputFirewallError(ValueError):
    """A payload crossed the runtime boundary with privileged information."""


def find_privileged_keys(value: Any, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            child = f"{path}.{key}"
            if _normalise_key(key) in FORBIDDEN_RUNTIME_FIELDS:
                findings.append(child)
            findings.extend(find_privileged_keys(nested, child))
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            findings.extend(find_privileged_keys(nested, f"{path}[{index}]"))
    return findings


def validate_maintenance_payload(payload: Mapping[str, Any]) -> MaintenanceInput:
    findings = find_privileged_keys(payload)
    if findings:
        preview = ", ".join(findings[:8])
        raise InputFirewallError(f"privileged runtime field(s) rejected: {preview}")
    try:
        return MaintenanceInput.model_validate(payload)
    except ValueError as exc:
        raise InputFirewallError(str(exc)) from exc


def maintenance_state_hash(payload: Mapping[str, Any]) -> str:
    """Validate then hash exactly the maintenance-visible representation."""

    model = validate_maintenance_payload(payload)
    return canonical_sha256(model.model_dump(mode="json"))


def assert_sidecar_independent(
    maintenance_payload: Mapping[str, Any],
    original_sidecar: Mapping[str, Any],
    mutated_sidecar: Mapping[str, Any],
) -> str:
    """Prove sidecar contents are not part of the maintenance state identity."""

    del original_sidecar, mutated_sidecar
    # The signature deliberately accepts sidecars only to make the contract
    # explicit.  Neither value is inspected or mixed into the state hash.
    return maintenance_state_hash(maintenance_payload)
