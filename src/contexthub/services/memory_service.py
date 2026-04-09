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
from contexthub.services.indexer_service import IndexerService
from contexthub.services.masking_service import MaskingService


class MemoryService:
    def __init__(
        self, indexer: IndexerService, acl: ACLService,
        masking: MaskingService, audit: AuditService | None = None,
    ):
        self._indexer = indexer
        self._acl = acl
        self._masking = masking
        self._audit = audit

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

        result = _row_to_context(row)

        if self._audit:
            await self._audit.log_strict(
                db, ctx.agent_id, "create", uri, "success",
                metadata={"context_type": "memory", "scope": "agent"},
            )
        return result

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
