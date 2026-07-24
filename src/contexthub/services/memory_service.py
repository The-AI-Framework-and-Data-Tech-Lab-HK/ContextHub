"""MemoryService: add, list, promote memories."""

from __future__ import annotations

import uuid

from contexthub.db.repository import ScopedRepo
from contexthub.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from contexthub.models.context import Context, Scope
from contexthub.models.memory import AddMemoryRequest, PromoteRequest
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.change_detection_service import ChangeDetectionService
from contexthub.services.conversation_extraction_service import (
    ConversationExtractionService,
)
from contexthub.services.dependency_discovery_service import (
    CandidateFact,
    DependencyDiscoveryService,
)
from contexthub.services.indexer_service import IndexerService
from contexthub.services.masking_service import MaskingService


class MemoryService:
    def __init__(
        self, indexer: IndexerService, acl: ACLService,
        masking: MaskingService, audit: AuditService | None = None,
        discovery: DependencyDiscoveryService | None = None,
        discovery_candidate_k: int = 8,
        detection: ChangeDetectionService | None = None,
        extractor: ConversationExtractionService | None = None,
    ):
        self._indexer = indexer
        self._acl = acl
        self._masking = masking
        self._audit = audit
        # Optional: discover semantic derived_from edges at write time. When None
        # (default), add_memory behaves exactly as before — no discovery, no edges.
        self._discovery = discovery
        self._discovery_candidate_k = discovery_candidate_k
        # Optional: zero-oracle change detection at write time. When None
        # (default), add_memory fires no supersede events — behaviour unchanged.
        self._detection = detection
        # Optional: extract facts from a raw conversation. Enables add_conversation.
        self._extractor = extractor

    async def add_memory(
        self, db: ScopedRepo, body: AddMemoryRequest, ctx: RequestContext
    ) -> Context:
        slug = f"mem-{uuid.uuid4().hex[:8]}"
        uri = f"ctx://agent/{ctx.agent_id}/memories/{slug}"

        generated = await self._indexer.generate("memory", body.content)

        try:
            row = await db.fetchrow(
                """
                INSERT INTO contexts
                    (uri, context_type, scope, owner_space, account_id,
                     l0_content, l1_content, l2_content, tags)
                VALUES ($1, 'memory', 'agent', $2, current_setting('app.account_id'),
                        $3, $4, $5, $6)
                RETURNING *
                """,
                uri,
                ctx.agent_id,
                generated.l0,
                generated.l1,
                body.content,
                body.tags,
            )
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise ConflictError(f"Memory {uri} already exists")
            raise

        await db.execute(
            """
            INSERT INTO change_events (context_id, account_id, change_type, actor)
            VALUES ($1, current_setting('app.account_id'), 'created', $2)
            """,
            row["id"],
            ctx.agent_id,
        )

        # Embedding consistency
        if generated.l0:
            await self._indexer.update_embedding(db, row["id"], generated.l0)

        # Optional: discover semantic derived_from edges from existing memories.
        if self._discovery is not None:
            await self._discover_edges(db, row["id"], body.content)

        # Optional: detect which existing memories this new fact supersedes and
        # fire a `modified` change_event on each — propagation then marks their
        # derived_from dependents stale. No detection injected => no events.
        if self._detection is not None:
            await self._detect_superseded(db, row["id"], body.content, ctx.agent_id)

        result = _row_to_context(row)

        if self._audit:
            await self._audit.log_strict(
                db, ctx.agent_id, "create", uri, "success",
                metadata={"context_type": "memory", "scope": "agent"},
            )
        return result

    async def _discover_edges(
        self, db: ScopedRepo, new_id: uuid.UUID, new_text: str
    ) -> None:
        """Discover derived_from sources for the new memory and persist edges.

        Candidates are the account's other active memories (most recent first).
        Uses the injected discovery service to decide which the new fact is
        derived from, then writes derived_from edges (idempotent).
        """
        rows = await db.fetch(
            """
            SELECT id, COALESCE(l2_content, l1_content, l0_content) AS text
            FROM contexts
            WHERE context_type = 'memory'
              AND status = 'active'
              AND id != $1
            ORDER BY updated_at DESC
            LIMIT $2
            """,
            new_id,
            self._discovery_candidate_k,
        )
        candidates = [
            CandidateFact(id=r["id"], text=r["text"]) for r in rows if r["text"]
        ]
        source_ids = await self._discovery.discover_sources(new_text, candidates)
        for src_id in source_ids:
            await db.execute(
                """
                INSERT INTO dependencies (dependent_id, dependency_id, dep_type)
                VALUES ($1, $2, 'derived_from')
                ON CONFLICT (dependent_id, dependency_id, dep_type) DO NOTHING
                """,
                new_id,
                src_id,
            )

    async def _detect_superseded(
        self, db: ScopedRepo, new_id: uuid.UUID, new_text: str, agent_id: str
    ) -> None:
        """Detect which existing memories new_text supersedes; fire `modified`.

        Candidate set = the account's other active memories (most recent first),
        same shape as _discover_edges. For each superseded node, emit one
        `modified` change_event; the propagation engine then marks its
        derived_from dependents stale.
        """
        rows = await db.fetch(
            """
            SELECT id, COALESCE(l2_content, l1_content, l0_content) AS text
            FROM contexts
            WHERE context_type = 'memory'
              AND status = 'active'
              AND id != $1
            ORDER BY updated_at DESC
            LIMIT $2
            """,
            new_id,
            self._discovery_candidate_k,
        )
        candidates = [
            CandidateFact(id=r["id"], text=r["text"]) for r in rows if r["text"]
        ]
        superseded = await self._detection.detect_superseded(new_text, candidates)
        for old_id in superseded:
            await db.execute(
                """
                INSERT INTO change_events
                    (context_id, account_id, change_type, actor, diff_summary)
                VALUES ($1, current_setting('app.account_id'), 'modified', $2, $3)
                """,
                old_id,
                agent_id,
                "superseded by new memory",
            )

    async def add_conversation(
        self, db: ScopedRepo, raw_text: str, ctx: RequestContext
    ) -> list[Context]:
        """Extract facts from a raw conversation, store each as a memory.

        Requires an injected ConversationExtractionService. Each extracted fact
        goes through the normal add_memory write path in order, so discovery and
        change detection (when injected) apply to every fact, and the timeline-
        incremental semantics are preserved (an earlier fact is stored before a
        later one can supersede it).
        """
        if self._extractor is None:
            raise BadRequestError("Conversation extraction is not configured")
        facts = await self._extractor.extract(raw_text)
        out: list[Context] = []
        for fact in facts:
            if not fact.text or not fact.text.strip():
                continue
            out.append(
                await self.add_memory(db, AddMemoryRequest(content=fact.text), ctx)
            )
        return out

    async def list_memories(
        self, db: ScopedRepo, ctx: RequestContext
    ) -> list[dict]:
        rows = await db.fetch(
            """
            SELECT uri, l0_content, status, version, tags, created_at, updated_at,
                   scope, owner_space
            FROM contexts
            WHERE context_type = 'memory'
              AND scope IN ('agent', 'team')
              AND status != 'deleted'
            ORDER BY updated_at DESC
            """,
        )
        visible_with_masks = await self._acl.filter_visible_with_acl(db, rows, ctx)
        result = [
            {
                "uri": r["uri"],
                "l0_content": self._masking.apply_masks(r["l0_content"], masks) if masks else r["l0_content"],
                "status": r["status"],
                "version": r["version"],
                "tags": list(r["tags"] or []),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r, masks in visible_with_masks
        ]

        if self._audit:
            await self._audit.log_best_effort(
                db, ctx.agent_id, "read", None, "success",
                metadata={"sub_action": "list_memories", "result_count": len(result)},
            )
        return result

    async def promote(
        self, db: ScopedRepo, body: PromoteRequest, ctx: RequestContext
    ) -> Context:
        # 1. Read source context
        source = await db.fetchrow(
            "SELECT * FROM contexts WHERE uri = $1 AND status != 'deleted'",
            body.uri,
        )
        if source is None:
            raise NotFoundError(f"Context {body.uri} not found")

        # 2. Must be memory
        if source["context_type"] != "memory":
            raise BadRequestError("Only memory contexts can be promoted")

        # 3. Must be current agent's private memory
        if source["scope"] != "agent" or source["owner_space"] != ctx.agent_id:
            raise ForbiddenError("Can only promote your own private memories")

        # 4. Check write permission on target team
        if not await self._acl.check_write_target(db, Scope.TEAM, body.target_team, ctx):
            raise ForbiddenError("No write permission on target team")

        # 5. Build target URI
        slug = body.uri.rsplit("/", 1)[-1]
        if body.target_team:
            target_uri = f"ctx://team/{body.target_team}/memories/shared_knowledge/{slug}"
        else:
            target_uri = f"ctx://team/memories/shared_knowledge/{slug}"

        # 6. Regenerate L0/L1
        generated = await self._indexer.generate("memory", source["l2_content"])

        # 7. Insert promoted context
        try:
            promoted = await db.fetchrow(
                """
                INSERT INTO contexts
                    (uri, context_type, scope, owner_space, account_id,
                     l0_content, l1_content, l2_content, tags)
                VALUES ($1, 'memory', 'team', $2, current_setting('app.account_id'),
                        $3, $4, $5, $6)
                RETURNING *
                """,
                target_uri,
                body.target_team,
                generated.l0,
                generated.l1,
                source["l2_content"],
                list(source["tags"] or []),
            )
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise ConflictError(f"Promoted memory {target_uri} already exists")
            raise

        # 8. Insert derived_from dependency
        await db.execute(
            """
            INSERT INTO dependencies (dependent_id, dependency_id, dep_type)
            VALUES ($1, $2, 'derived_from')
            """,
            promoted["id"],
            source["id"],
        )

        # 9. Insert change event
        await db.execute(
            """
            INSERT INTO change_events
                (context_id, account_id, change_type, actor, metadata)
            VALUES ($1, current_setting('app.account_id'), 'created', $2, $3)
            """,
            promoted["id"],
            ctx.agent_id,
            f'{{"promoted_from": "{body.uri}"}}',
        )

        # Embedding consistency for promoted memory
        if generated.l0:
            await self._indexer.update_embedding(db, promoted["id"], generated.l0)

        result = _row_to_context(promoted)

        if self._audit:
            await self._audit.log_strict(
                db, ctx.agent_id, "promote", target_uri, "success",
                metadata={"source_uri": body.uri, "target_team": body.target_team},
            )
        return result


def _row_to_context(row) -> Context:
    return Context(
        id=row["id"],
        uri=row["uri"],
        context_type=row["context_type"],
        scope=row["scope"],
        owner_space=row["owner_space"],
        account_id=row["account_id"],
        l0_content=row["l0_content"],
        l1_content=row["l1_content"],
        l2_content=row["l2_content"],
        file_path=row["file_path"],
        status=row["status"],
        version=row["version"],
        tags=list(row["tags"] or []),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_accessed_at=row["last_accessed_at"],
        stale_at=row["stale_at"],
        archived_at=row["archived_at"],
        deleted_at=row["deleted_at"],
        active_count=row["active_count"],
        adopted_count=row["adopted_count"],
        ignored_count=row["ignored_count"],
    )
