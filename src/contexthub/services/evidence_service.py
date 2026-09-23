"""Resolve snapshots against the existing content-version table; no new content DB."""

from datetime import datetime
from uuid import UUID, uuid4

from contexthub.db.repository import ScopedRepo
from contexthub.models.knowledge import (
    ContractError,
    ContextVersion,
    EvidenceRef,
    SealRef,
    SourceSnapshot,
    text_hash,
)
from contexthub.services.artifact_service import ArtifactService, require_account


class EvidenceService:
    def __init__(self, artifacts: ArtifactService | None = None):
        self.artifacts = artifacts or ArtifactService()

    async def _version(self, db, account_id, source):
        await require_account(db, account_id)
        row = await db.fetchrow(
            """SELECT v.* FROM context_versions v JOIN contexts c ON c.id=v.context_id
            WHERE c.account_id=$1 AND v.context_id=$2 AND v.version=$3""",
            account_id,
            source.context_id,
            source.version,
        )
        if row is None:
            raise ContractError("source_version_missing")
        return row

    async def snapshot(
        self,
        db: ScopedRepo,
        *,
        account_id: str,
        source: ContextVersion,
        content_level: str,
        cutoff_at: datetime,
        snapshot_id: UUID | None = None,
    ) -> SealRef:
        if content_level not in ("l0_content", "l1_content", "l2_content"):
            raise ContractError("invalid_content_level")
        row = await self._version(db, account_id, source)
        if row[content_level] is None:
            raise ContractError("source_content_missing")
        obj = SourceSnapshot(
            account_id=account_id,
            id=snapshot_id or uuid4(),
            source=source,
            content_level=content_level,
            ingested_at=row["created_at"],
            cutoff_at=cutoff_at,
            content_sha256=text_hash(row[content_level]),
        )
        return await self.artifacts.seal(db, obj)

    async def read_source(self, db: ScopedRepo, source: SourceSnapshot) -> str:
        row = await self._version(db, source.account_id, source.source)
        text = row[source.content_level]
        if text is None:
            raise ContractError("source_content_missing")
        if (
            row["created_at"] != source.ingested_at
            or text_hash(text) != source.content_sha256
        ):
            raise ContractError("source_version_mismatch")
        return text

    async def reference(
        self,
        db: ScopedRepo,
        source: SealRef,
        *,
        start: int,
        end: int,
        provenance: tuple[str, ...],
    ) -> EvidenceRef:
        obj = await self.artifacts.get(db, source)
        if not isinstance(obj, SourceSnapshot):
            raise ContractError("reference_kind_mismatch")
        text = await self.read_source(db, obj)
        if start < 0 or end > len(text) or end <= start:
            raise ContractError("invalid_span")
        excerpt = text[start:end]
        return EvidenceRef(
            source=source,
            start=start,
            end=end,
            excerpt=excerpt,
            excerpt_sha256=text_hash(excerpt),
            provenance=provenance,
        )

    async def resolve(self, db: ScopedRepo, ref: EvidenceRef) -> str:
        obj = await self.artifacts.get(db, ref.source)
        if not isinstance(obj, SourceSnapshot):
            raise ContractError("reference_kind_mismatch")
        text = await self.read_source(db, obj)
        if ref.end > len(text) or text[ref.start : ref.end] != ref.excerpt:
            raise ContractError("source_span_mismatch")
        return ref.excerpt
