"""Tenant-scoped immutable storage. Uses the caller's existing transaction."""

from uuid import UUID

from pydantic import BaseModel

from contexthub.db.repository import ScopedRepo
from contexthub.models.knowledge import (
    ContractError,
    ContextVersion,
    EvidenceRef,
    GraphSnapshot,
    PropositionArtifact,
    Sealed,
    SealRef,
    SourceSnapshot,
    canonical_hash,
)
from contexthub.models.maintenance import (
    Candidate,
    CommitProof,
    EvidenceBundle,
    HardCheckResult,
    MaintenanceSnapshot,
    VerificationResult,
    WorkState,
)

MODELS = {
    m.model_fields["kind"].default: m
    for m in (
        SourceSnapshot,
        PropositionArtifact,
        GraphSnapshot,
        MaintenanceSnapshot,
        EvidenceBundle,
        Candidate,
        HardCheckResult,
        VerificationResult,
        CommitProof,
    )
}


def walk(value):
    yield value
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from walk(getattr(value, name))
    elif isinstance(value, tuple):
        for item in value:
            yield from walk(item)


async def require_account(db: ScopedRepo, account_id: str):
    if (
        await db.fetchval("SELECT current_setting('app.account_id', true)")
        != account_id
    ):
        raise ContractError("tenant_mismatch")


class ArtifactService:
    async def get(self, db: ScopedRepo, ref: SealRef) -> Sealed:
        await require_account(db, ref.account_id)
        table = (
            "context_refresh_proofs"
            if ref.kind == "commit_proof"
            else "knowledge_artifacts"
        )
        row = await db.fetchrow(
            f"SELECT payload, sha256 FROM {table} WHERE account_id=$1 AND id=$2",
            ref.account_id,
            ref.id,
        )
        if row is None:
            raise ContractError("artifact_missing")
        if row["sha256"] != ref.sha256 or canonical_hash(row["payload"]) != ref.sha256:
            raise ContractError("artifact_hash_mismatch")
        model = MODELS.get(ref.kind)
        if model is None or row["payload"].get("kind") != ref.kind:
            raise ContractError("reference_kind_mismatch")
        obj = model.model_validate(row["payload"])
        if obj.id != ref.id or obj.account_id != ref.account_id:
            raise ContractError("artifact_identity_mismatch")
        return obj

    async def validate(self, db: ScopedRepo, obj: Sealed):
        from contexthub.services.evidence_service import EvidenceService

        await require_account(db, obj.account_id)
        evidence = EvidenceService(self)
        for value in walk(obj):
            if isinstance(value, SealRef):
                if value.account_id != obj.account_id:
                    raise ContractError("tenant_mismatch")
                await self.get(db, value)
            elif isinstance(value, ContextVersion):
                found = await db.fetchval(
                    """SELECT 1 FROM context_versions v JOIN contexts c ON c.id=v.context_id
                    WHERE c.account_id=$1 AND v.context_id=$2 AND v.version=$3""",
                    obj.account_id,
                    value.context_id,
                    value.version,
                )
                if not found:
                    raise ContractError("context_version_missing")
            elif isinstance(value, EvidenceRef):
                await evidence.resolve(db, value)
        if isinstance(obj, SourceSnapshot):
            await evidence.read_source(db, obj)
        if isinstance(obj, MaintenanceSnapshot):
            event = await db.fetchval(
                "SELECT 1 FROM change_events WHERE account_id=$1 AND event_id=$2",
                obj.account_id,
                obj.event_id,
            )
            if not event:
                raise ContractError("event_missing")
            for cause in obj.cause_ids:
                if not await db.fetchval(
                    "SELECT 1 FROM change_events WHERE account_id=$1 AND event_id=$2",
                    obj.account_id,
                    cause,
                ):
                    raise ContractError("cause_event_missing")
            graph = await self.get(db, obj.graph)
            if obj.target not in graph.nodes or any(
                u not in graph.nodes for u in obj.necessary_upstreams
            ):
                raise ContractError("graph_version_mismatch")
            for ref in obj.allowed_sources:
                source = await self.get(db, ref)
                if source.ingested_at > obj.cutoff_at:
                    raise ContractError("source_after_cutoff")
        if isinstance(obj, EvidenceBundle):
            snapshot = await self.get(db, obj.maintenance)
            for value in walk(obj):
                if (
                    isinstance(value, EvidenceRef)
                    and value.source not in snapshot.allowed_sources
                ):
                    raise ContractError("source_outside_scope")
            if any(
                issue.source not in snapshot.allowed_sources for issue in obj.issues
            ):
                raise ContractError("source_outside_scope")
        if isinstance(obj, Candidate):
            bundle = await self.get(db, obj.bundle)
            if bundle.maintenance != obj.maintenance:
                raise ContractError("maintenance_reference_mismatch")
            if any(
                e not in tuple(r.evidence for r in bundle.read) for e in obj.evidence
            ):
                raise ContractError("evidence_not_read")
        if isinstance(obj, VerificationResult):
            candidate = await self.get(db, obj.candidate)
            if candidate.bundle != obj.bundle:
                raise ContractError("bundle_reference_mismatch")
            if candidate.generator_call_id == obj.verifier_call_id:
                raise ContractError("generator_cannot_verify")

    async def seal(self, db: ScopedRepo, obj: Sealed) -> SealRef:
        # Commit receipts have a separate audit-only interface; a generator cannot
        # accidentally pass a proof through the ordinary artifact entry point.
        if isinstance(obj, CommitProof):
            raise ContractError("commit_proof_requires_commit_path")
        if type(obj) not in MODELS.values():
            raise ContractError("unsupported_artifact")
        obj = type(obj).model_validate(obj.model_dump(mode="json"))
        await self.validate(db, obj)
        ref = obj.reference()
        await db.execute(
            """INSERT INTO knowledge_artifacts(account_id,id,kind,schema_version,sha256,payload)
            VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(account_id,id) DO NOTHING""",
            obj.account_id,
            obj.id,
            obj.kind,
            obj.schema_version,
            ref.sha256,
            obj.model_dump(mode="json"),
        )
        await self.get(db, ref)  # exact duplicate is idempotent; changed ID fails
        return ref

    async def create_work(
        self, db: ScopedRepo, work_id: UUID, snapshot: SealRef
    ) -> WorkState:
        if snapshot.kind != "maintenance":
            raise ContractError("reference_kind_mismatch")
        await self.get(db, snapshot)
        state = WorkState(revision=1, status="pending")
        await db.execute(
            """INSERT INTO maintenance_work_items(account_id,id,snapshot_id,revision,status,state)
            VALUES($1,$2,$3,1,'pending',$4) ON CONFLICT DO NOTHING""",
            snapshot.account_id,
            work_id,
            snapshot.id,
            state.model_dump(mode="json"),
        )
        row = await db.fetchrow(
            "SELECT snapshot_id,state FROM maintenance_work_items WHERE account_id=$1 AND id=$2",
            snapshot.account_id,
            work_id,
        )
        if row is None or row["snapshot_id"] != snapshot.id:
            raise ContractError("work_identity_conflict")
        return WorkState.model_validate(row["state"])

    async def get_work(
        self, db: ScopedRepo, account_id: str, work_id: UUID
    ) -> WorkState:
        await require_account(db, account_id)
        row = await db.fetchrow(
            "SELECT state FROM maintenance_work_items WHERE account_id=$1 AND id=$2",
            account_id,
            work_id,
        )
        if row is None:
            raise ContractError("work_missing")
        return WorkState.model_validate(row["state"])

    async def update_work(
        self,
        db: ScopedRepo,
        account_id: str,
        work_id: UUID,
        expected_revision: int,
        state: WorkState,
    ) -> WorkState:
        await require_account(db, account_id)
        if state.revision != expected_revision + 1:
            raise ContractError("work_revision_conflict")
        old = await self.get_work(db, account_id, work_id)
        if old.revision != expected_revision or old.status in (
            "updated",
            "unchanged",
            "unresolved",
        ):
            raise ContractError("work_revision_conflict")
        if state.status == "pending":
            raise ContractError("invalid_work_transition")
        # S6 will atomically attach a commit receipt in its guarded commit path.
        if state.proof is not None:
            raise ContractError("commit_path_not_implemented")
        updated = await db.fetchval(
            """UPDATE maintenance_work_items SET revision=$4,status=$5,state=$6
            WHERE account_id=$1 AND id=$2 AND revision=$3 RETURNING revision""",
            account_id,
            work_id,
            expected_revision,
            state.revision,
            state.status,
            state.model_dump(mode="json"),
        )
        if updated is None:
            raise ContractError("work_revision_conflict")
        return state

    async def archive_commit_proof(self, db: ScopedRepo, proof: CommitProof) -> SealRef:
        """Archive an audit receipt only; this NEVER authorizes/restores freshness.

        S6 owns atomic content writes, drift checks and cause removal. A receipt
        can be stored only against consistent, separately sealed check records.
        """
        proof = CommitProof.model_validate(proof.model_dump(mode="json"))
        await self.validate(db, proof)
        candidate = await self.get(db, proof.candidate)
        check = await self.get(db, proof.hard_check)
        verdict = await self.get(db, proof.verification)
        snapshot = await self.get(db, proof.maintenance)
        if (
            candidate.maintenance != proof.maintenance
            or check.candidate != proof.candidate
            or verdict.candidate != proof.candidate
        ):
            raise ContractError("proof_reference_mismatch")
        if any(
            item.verdict != "PASS"
            for item in (
                *check.checks,
                verdict.applicability,
                verdict.complete_support,
                verdict.conflicts_resolved,
            )
        ):
            raise ContractError("proof_checks_not_passed")
        if candidate.generator_call_id == verdict.verifier_call_id:
            raise ContractError("generator_cannot_verify")
        if (
            proof.committed_target.context_id != snapshot.target.context_id
            or proof.committed_target.version
            != snapshot.target.version + (candidate.outcome == "updated")
            or proof.checked_upstreams != snapshot.necessary_upstreams
            or proof.checked_sources != snapshot.allowed_sources
            or proof.cleared_cause_ids != snapshot.cause_ids
        ):
            raise ContractError("proof_version_mismatch")
        bundle = await self.get(db, candidate.bundle)
        if not bundle.complete or any(not c.resolved for c in bundle.conflicts):
            raise ContractError("proof_evidence_incomplete")
        ref = proof.reference()
        await db.execute(
            """INSERT INTO context_refresh_proofs
            (account_id,id,target_id,target_version,sha256,payload) VALUES($1,$2,$3,$4,$5,$6)
            ON CONFLICT(account_id,id) DO NOTHING""",
            proof.account_id,
            proof.id,
            proof.committed_target.context_id,
            proof.committed_target.version,
            ref.sha256,
            proof.model_dump(mode="json"),
        )
        await self.get(db, ref)
        return ref
