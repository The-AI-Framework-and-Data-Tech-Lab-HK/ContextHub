"""Versioned, immutable core contracts. Hashes use canonical UTF-8 JSON (v1)."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Name = Annotated[str, Field(min_length=1)]
Positive = Annotated[int, Field(strict=True, ge=1)]
Nonnegative = Annotated[int, Field(strict=True, ge=0)]


class ContractError(ValueError):
    """Stable machine-readable code; message never contains source text/secrets."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1


def canonical_hash(value: BaseModel | dict | list) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SealRef(Contract):
    account_id: Name
    id: UUID
    kind: Name
    sha256: Hash


class Sealed(Contract):
    account_id: Name
    id: UUID

    def reference(self) -> SealRef:
        return SealRef(
            account_id=self.account_id,
            id=self.id,
            kind=self.kind,
            sha256=canonical_hash(self),
        )


class ContextVersion(Contract):
    context_id: UUID
    version: Positive


class SourceSnapshot(Sealed):
    kind: Literal["source"] = "source"
    source: ContextVersion
    content_level: Literal["l0_content", "l1_content", "l2_content"]
    ingested_at: AwareDatetime
    cutoff_at: AwareDatetime
    content_sha256: Hash

    @model_validator(mode="after")
    def cutoff(self):
        if self.ingested_at > self.cutoff_at:
            raise ContractError("source_after_cutoff")
        return self


class EvidenceRef(Contract):
    source: SealRef
    start: Nonnegative
    end: Positive
    excerpt: Name
    excerpt_sha256: Hash
    provenance: tuple[Name, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def span(self):
        if self.source.kind != "source" or self.end <= self.start:
            raise ContractError("invalid_span")
        if (
            len(self.excerpt) != self.end - self.start
            or text_hash(self.excerpt) != self.excerpt_sha256
        ):
            raise ContractError("excerpt_mismatch")
        return self


class Proposition(Contract):
    id: UUID
    text: Name
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)


class PropositionArtifact(Sealed):
    kind: Literal["propositions"] = "propositions"
    sources: tuple[SealRef, ...] = Field(min_length=1)
    extraction_config_id: Name
    extraction_config_sha256: Hash
    propositions: tuple[Proposition, ...] = ()
    status: Literal["complete", "pending"]
    failure_reason: Name | None = None

    @model_validator(mode="after")
    def consistency(self):
        if any(s.kind != "source" for s in self.sources):
            raise ContractError("reference_kind_mismatch")
        if (self.status == "pending") != (self.failure_reason is not None):
            raise ContractError("failure_reason_required")
        if len({p.id for p in self.propositions}) != len(self.propositions):
            raise ContractError("duplicate_proposition")
        if any(
            e.source not in self.sources for p in self.propositions for e in p.evidence
        ):
            raise ContractError("undeclared_source")
        return self


class DependencyEdge(Contract):
    # Direction: dependency (upstream) -> dependent (downstream).
    upstream: ContextVersion
    downstream: ContextVersion
    semantic_basis: Name
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)
    provenance: tuple[Name, ...] = Field(min_length=1)


class PendingDependency(Contract):
    upstream: ContextVersion
    downstream: ContextVersion
    reason: Name


class GraphSnapshot(Sealed):
    kind: Literal["graph"] = "graph"
    graph_version: Positive
    nodes: tuple[ContextVersion, ...]
    edges: tuple[DependencyEdge, ...] = ()
    pending: tuple[PendingDependency, ...] = ()
    config_sha256: Hash

    @model_validator(mode="after")
    def endpoints(self):
        if len({n.context_id for n in self.nodes}) != len(self.nodes):
            raise ContractError("duplicate_node")
        pairs = [(e.upstream, e.downstream) for e in self.edges]
        if len(set(pairs)) != len(pairs):
            raise ContractError("duplicate_edge")
        for edge in (*self.edges, *self.pending):
            if (
                edge.upstream not in self.nodes
                or edge.downstream not in self.nodes
                or edge.upstream == edge.downstream
            ):
                raise ContractError("invalid_endpoint")
        # A published dependency graph is a DAG, including isolated nodes.
        remaining = set(self.nodes)
        while remaining:
            roots = {
                n
                for n in remaining
                if not any(d == n and u in remaining for u, d in pairs)
            }
            if not roots:
                raise ContractError("cyclic_graph")
            remaining -= roots
        return self
