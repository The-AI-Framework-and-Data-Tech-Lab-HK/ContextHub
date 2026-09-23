from uuid import uuid4

import pytest
from pydantic import ValidationError

from contexthub.models.knowledge import (
    ContextVersion,
    ContractError,
    DependencyEdge,
    EvidenceRef,
    GraphSnapshot,
    PropositionArtifact,
    SealRef,
    SourceSnapshot,
    canonical_hash,
    text_hash,
)
from contexthub.models.maintenance import (
    Candidate,
    EvidenceBundle,
    ReadEntry,
    WorkState,
)
from knowledge_helpers import HASH, NOW


def source_ref():
    return SealRef(account_id="s2-a", id=uuid4(), kind="source", sha256=HASH)


def evidence():
    return EvidenceRef(
        source=source_ref(),
        start=0,
        end=3,
        excerpt="甲乙丙",
        excerpt_sha256=text_hash("甲乙丙"),
        provenance=("dialogue",),
    )


def test_hash_roundtrip_and_frozen_contract():
    source = SourceSnapshot(
        account_id="a",
        id=uuid4(),
        source=ContextVersion(context_id=uuid4(), version=1),
        content_level="l2_content",
        ingested_at=NOW,
        cutoff_at=NOW,
        content_sha256=HASH,
    )
    assert source == SourceSnapshot.model_validate_json(source.model_dump_json())
    assert canonical_hash(source) == canonical_hash(source.model_dump(mode="json"))
    assert canonical_hash({"b": 1, "a": 2}) == canonical_hash({"a": 2, "b": 1})
    with pytest.raises(ValidationError):
        source.content_sha256 = "0" * 64
    with pytest.raises(ValidationError):
        SourceSnapshot.model_validate({**source.model_dump(), "task_type": "hidden"})
    with pytest.raises(ValidationError):
        SourceSnapshot.model_validate({**source.model_dump(), "schema_version": 2})


@pytest.mark.parametrize(
    "changes",
    ({"end": 4}, {"start": -1}, {"excerpt_sha256": "0" * 64}, {"provenance": ()}),
)
def test_span_invalid(changes):
    with pytest.raises((ValidationError, ContractError)):
        EvidenceRef.model_validate({**evidence().model_dump(), **changes})


def test_pending_is_not_empty_success():
    with pytest.raises(ValidationError):
        PropositionArtifact(
            account_id="a",
            id=uuid4(),
            sources=(source_ref(),),
            extraction_config_id="x",
            extraction_config_sha256=HASH,
            status="pending",
        )
    with pytest.raises(ValidationError):
        WorkState(revision=1, status="unresolved")
    with pytest.raises(ValidationError):
        WorkState(revision=1, status="updated")


def test_graph_endpoints_and_cycles():
    a, b = [ContextVersion(context_id=uuid4(), version=1) for _ in range(2)]
    edge = DependencyEdge(
        upstream=a,
        downstream=b,
        semantic_basis="depends",
        evidence=(evidence(),),
        provenance=("explicit",),
    )
    args = dict(account_id="s2-a", id=uuid4(), graph_version=1, config_sha256=HASH)
    GraphSnapshot(**args, nodes=(a, b), edges=(edge,))
    for nodes, edges in [
        ((a,), (edge,)),
        ((a, b), (edge, edge)),
        ((a, b), (edge, edge.model_copy(update={"upstream": b, "downstream": a}))),
    ]:
        with pytest.raises(ValidationError):
            GraphSnapshot(**args, nodes=nodes, edges=edges)


def test_bundle_completeness_duplicates_and_order():
    entry = ReadEntry(evidence=evidence(), ordinal=0, page=0, chunk=0)
    args = dict(
        account_id="s2-a",
        id=uuid4(),
        maintenance=source_ref().model_copy(update={"kind": "maintenance"}),
        scope_id="local",
        required=(entry,),
        read=(entry,),
        complete=True,
        truncated=False,
        ordering="ordinal",
        page_size=1,
        chunk_size=100,
    )
    EvidenceBundle(**args)
    for changes in (
        {"read": ()},
        {"truncated": True},
        {"next_cursor": "more"},
        {"required": (entry, entry)},
    ):
        with pytest.raises(ValidationError):
            EvidenceBundle(**{**args, **changes})
    EvidenceBundle(**{**args, "read": (), "complete": False})


def test_candidate_cannot_supply_verdict_or_proof():
    with pytest.raises(ValidationError):
        Candidate(
            account_id="a",
            id=uuid4(),
            maintenance=source_ref().model_copy(update={"kind": "maintenance"}),
            bundle=source_ref().model_copy(update={"kind": "evidence_bundle"}),
            outcome="unchanged",
            proposed_content="same",
            evidence=(),
            generator_call_id=uuid4(),
            generator_config_sha256=HASH,
            verdict="PASS",
            proof="fresh",
        )
