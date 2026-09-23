from __future__ import annotations

from copy import deepcopy

import pytest

from integrations.memebench.patha.firewall import (
    InputFirewallError,
    assert_sidecar_independent,
    maintenance_state_hash,
    validate_maintenance_payload,
)
from integrations.memebench.patha.schemas import SCHEMA_VERSION, canonical_sha256


def _payload() -> dict:
    messages = [{"role": "user", "content": "The source text is visible."}]
    source = {
        "source_id": "src_" + "2" * 24,
        "ordinal": 0,
        "source_version": 1,
        "timestamp": "2026-01-01T00:00:00Z",
        "messages": messages,
        "content_sha256": canonical_sha256(messages),
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": "meme_filler32k@" + "a" * 12,
        "episode_id": "ep_" + "1" * 24,
        "sources": [source],
    }
    payload["input_sha256"] = canonical_sha256(payload)
    return payload


@pytest.mark.parametrize(
    "field",
    [
        "task_type",
        "hop",
        "gold_answer",
        "question",
        "dependency_edges_used",
        "root_change",
        "gold_facts",
        "evidence_type",
    ],
)
def test_firewall_rejects_privileged_fields_at_any_depth(field: str):
    payload = _payload()
    payload["sources"][0][field] = "leak"
    with pytest.raises(InputFirewallError, match="privileged"):
        validate_maintenance_payload(payload)


def test_firewall_rejects_semantic_ids_and_extra_fields():
    payload = _payload()
    payload["episode_id"] = "pl_001_Cas_hop2"
    payload["input_sha256"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "input_sha256"}
    )
    with pytest.raises(InputFirewallError, match="opaque"):
        validate_maintenance_payload(payload)


def test_mutating_sidecar_cannot_change_maintenance_state_hash():
    payload = _payload()
    original = {"gold_answer": "A", "hop": 1}
    mutated = {"gold_answer": "B", "hop": 2, "task_type": "Abs"}
    expected = maintenance_state_hash(payload)
    assert assert_sidecar_independent(payload, original, mutated) == expected
    assert maintenance_state_hash(deepcopy(payload)) == expected
