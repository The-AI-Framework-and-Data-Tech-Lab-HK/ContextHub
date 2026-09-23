"""S2 records only: none of these DTOs can authorize a fresh transition."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from contexthub.models.knowledge import (
    Contract,
    ContractError,
    ContextVersion,
    EvidenceRef,
    Hash,
    Name,
    Nonnegative,
    Positive,
    Sealed,
    SealRef,
)


class MaintenanceSnapshot(Sealed):
    kind: Literal["maintenance"] = "maintenance"
    event_id: UUID
    target: ContextVersion
    necessary_upstreams: tuple[ContextVersion, ...] = ()
    cause_ids: tuple[UUID, ...] = Field(min_length=1)
    graph: SealRef
    allowed_sources: tuple[SealRef, ...]
    cutoff_at: AwareDatetime
    policy_sha256: Hash

    @model_validator(mode="after")
    def references(self):
        if self.graph.kind != "graph" or any(
            s.kind != "source" for s in self.allowed_sources
        ):
            raise ContractError("reference_kind_mismatch")
        if len(set(self.cause_ids)) != len(self.cause_ids) or len(
            set(self.allowed_sources)
        ) != len(self.allowed_sources):
            raise ContractError("duplicate_manifest_entry")
        if len({u.context_id for u in self.necessary_upstreams}) != len(
            self.necessary_upstreams
        ):
            raise ContractError("duplicate_upstream")
        return self


class ReadEntry(Contract):
    evidence: EvidenceRef
    ordinal: Nonnegative
    page: Nonnegative
    chunk: Nonnegative


class ReadIssue(Contract):
    source: SealRef
    code: Name


class KnownConflict(Contract):
    id: UUID
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)
    description: Name
    resolved: bool = False


class EvidenceBundle(Sealed):
    kind: Literal["evidence_bundle"] = "evidence_bundle"
    maintenance: SealRef
    scope_id: Name
    required: tuple[ReadEntry, ...]
    read: tuple[ReadEntry, ...]
    complete: bool
    truncated: bool
    next_cursor: str | None = None
    ordering: Name
    page_size: Positive
    chunk_size: Positive
    issues: tuple[ReadIssue, ...] = ()
    conflicts: tuple[KnownConflict, ...] = ()

    @model_validator(mode="after")
    def manifest(self):
        if self.maintenance.kind != "maintenance":
            raise ContractError("reference_kind_mismatch")
        for manifest in (self.required, self.read):
            if len({r.ordinal for r in manifest}) != len(manifest):
                raise ContractError("duplicate_manifest_entry")
            if list(manifest) != sorted(manifest, key=lambda r: r.ordinal):
                raise ContractError("manifest_order_mismatch")
        if any(r not in self.required for r in self.read):
            raise ContractError("unexpected_read")
        if self.complete and (
            self.read != self.required
            or self.truncated
            or self.issues
            or self.next_cursor is not None
        ):
            raise ContractError("incomplete_manifest")
        return self


class Candidate(Sealed):
    kind: Literal["candidate"] = "candidate"
    maintenance: SealRef
    bundle: SealRef
    outcome: Literal["updated", "unchanged"]
    proposed_content: Name
    evidence: tuple[EvidenceRef, ...]
    generator_call_id: UUID
    generator_config_sha256: Hash
    rule_version: str | None = None

    @model_validator(mode="after")
    def refs(self):
        if (
            self.maintenance.kind != "maintenance"
            or self.bundle.kind != "evidence_bundle"
        ):
            raise ContractError("reference_kind_mismatch")
        return self


class CheckItem(Contract):
    code: Name
    verdict: Literal["PASS", "FAIL", "UNKNOWN"]
    reason: Name


class HardCheckResult(Sealed):
    kind: Literal["hard_check"] = "hard_check"
    candidate: SealRef
    checker_version: Name
    checks: tuple[CheckItem, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def refs(self):
        if self.candidate.kind != "candidate":
            raise ContractError("reference_kind_mismatch")
        if len({c.code for c in self.checks}) != len(self.checks):
            raise ContractError("duplicate_check")
        return self


class VerificationResult(Sealed):
    kind: Literal["verification"] = "verification"
    candidate: SealRef
    bundle: SealRef
    verifier_call_id: UUID
    verifier_config_sha256: Hash
    applicability: CheckItem
    complete_support: CheckItem
    conflicts_resolved: CheckItem

    @model_validator(mode="after")
    def refs(self):
        if self.candidate.kind != "candidate" or self.bundle.kind != "evidence_bundle":
            raise ContractError("reference_kind_mismatch")
        return self


class CommitProof(Sealed):
    """Audit evidence, not a capability. S6 must perform guarded commit separately."""

    kind: Literal["commit_proof"] = "commit_proof"
    authority: Literal["audit_only"] = "audit_only"
    candidate: SealRef
    hard_check: SealRef
    verification: SealRef
    maintenance: SealRef
    committed_target: ContextVersion
    checked_upstreams: tuple[ContextVersion, ...]
    checked_sources: tuple[SealRef, ...]
    cleared_cause_ids: tuple[UUID, ...]
    committed_at: AwareDatetime
    commit_code_version: Name

    @model_validator(mode="after")
    def refs(self):
        for ref, kind in (
            (self.candidate, "candidate"),
            (self.hard_check, "hard_check"),
            (self.verification, "verification"),
            (self.maintenance, "maintenance"),
        ):
            if ref.kind != kind:
                raise ContractError("reference_kind_mismatch")
        return self


class WorkState(Contract):
    """CAS revision of work progress; never a ContextStatus/validity write."""

    revision: Positive
    status: Literal["pending", "running", "updated", "unchanged", "unresolved"]
    reason: Name | None = None
    proof: SealRef | None = None

    @model_validator(mode="after")
    def result(self):
        if self.status == "unresolved" and self.reason is None:
            raise ContractError("failure_reason_required")
        if (self.status in ("updated", "unchanged")) != (self.proof is not None):
            raise ContractError("proof_required")
        if self.proof and self.proof.kind != "commit_proof":
            raise ContractError("reference_kind_mismatch")
        return self
