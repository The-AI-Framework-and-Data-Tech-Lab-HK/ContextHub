"""Lifecycle service: policy management and context status transitions."""

from __future__ import annotations

from enum import StrEnum
import hashlib
import json
from uuid import UUID

from contexthub.db.repository import ScopedRepo
from contexthub.errors import ConflictError, NotFoundError
from contexthub.models.lifecycle import LifecyclePolicy
from contexthub.models.request import RequestContext
from contexthub.services.audit_service import AuditService
from contexthub.services.indexer_service import IndexerService

SYSTEM_ACTORS = {
    "lifecycle_scheduler": "lifecycle_scheduler",
    "propagation_engine": "propagation_engine",
}

DEFAULT_LIFECYCLE_POLICIES = (
    ("memory", "agent", 90, 30, 180),
    ("memory", "team", 0, 60, 0),
    ("resource", "datalake", 0, 0, 0),
    ("resource", "team", 0, 0, 0),
    ("skill", "team", 0, 90, 0),
)


def make_system_context(account_id: str, system_actor: str) -> RequestContext:
    return RequestContext(
        account_id=account_id,
        agent_id=system_actor,
    )


class LifecycleService:
    def __init__(
        self,
        audit: AuditService | None = None,
        indexer: IndexerService | None = None,
    ):
        self._audit = audit
        self._indexer = indexer

    async def mark_stale(
        self,
        db: ScopedRepo,
        context_id: UUID,
        reason: str,
        ctx: RequestContext,
        source_event: dict | None = None,
    ) -> None:
        row = await self._fetch_context_row(db, context_id, extra_columns=("version",))
        if row["status"] not in ("active", "stale"):
            return

        result = await db.execute(
            """
            UPDATE contexts
            SET status = 'stale',
                validity_status = 'stale',
                validity_reason = $2,
                stale_at = NOW(),
                updated_at = NOW()
            WHERE id = $1 AND status = 'active'
            """,
            context_id,
            reason,
        )
        transitioned = result != "UPDATE 0"
        # A repeated manual stale mark without a structured cause is idempotent.
        # Structured propagation causes must still be recorded independently so
        # multi-cause invalidation remains correct.
        if not transitioned and source_event is None:
            return

        parent_event_id = source_event.get("event_id") if source_event else None
        cause_identity = str(parent_event_id) if parent_event_id else hashlib.sha256(
            reason.encode("utf-8")
        ).hexdigest()
        idempotency_key = (
            f"marked-stale:{context_id}:{row['version']}:cause:{cause_identity}"
        )
        event_id = await db.fetchval(
            """
            INSERT INTO change_events
                (context_id, account_id, change_type, actor, diff_summary,
                 idempotency_key, source_version, graph_scope, parent_event_id,
                 root_event_id, plan_id, depth, metadata)
            VALUES (
              $1::uuid, $2, 'marked_stale', $3, $4, $5, $6,
              'dependency-closure', $7::uuid,
              COALESCE($8::uuid, $7::uuid), $9::uuid, $10, $11::jsonb
            )
            ON CONFLICT (account_id, idempotency_key) DO UPDATE
              SET updated_at = NOW()
            RETURNING event_id
            """,
            context_id,
            ctx.account_id,
            ctx.agent_id,
            reason,
            idempotency_key,
            row["version"],
            parent_event_id,
            source_event.get("root_event_id") if source_event else None,
            source_event.get("plan_id") if source_event else None,
            int(source_event.get("depth") or 0) + 1 if source_event else 0,
            (
                source_event.get("metadata")
                if isinstance(source_event.get("metadata"), str)
                else json.dumps(source_event.get("metadata") or {})
            )
            if source_event
            else "{}",
        )
        # Read barrier: every known structured descendant and identity-linked copy
        # becomes unserviceable in the same transaction as the source stale event.
        # This is deliberately structural; no content or entity-name matching occurs.
        await db.execute(
            """
            WITH RECURSIVE invalid(node_id) AS (
              SELECT $1::uuid
              UNION
              SELECT edge.node_id
                FROM invalid i
                JOIN LATERAL (
                  SELECT d.dependent_id AS node_id
                    FROM dependencies d
                   WHERE d.dependency_id = i.node_id
                  UNION
                  SELECT r.context_id AS node_id
                    FROM context_relations r
                   WHERE r.related_context_id = i.node_id
                     AND r.relation_type IN (
                       'alias_of', 'duplicate_of', 'materialized_from'
                     )
                ) edge ON TRUE
            )
            UPDATE contexts c
               SET validity_status = CASE
                     WHEN c.id = $1 THEN 'stale' ELSE 'invalid'
                   END,
                   validity_reason = CASE
                     WHEN c.id = $1 THEN $2
                     ELSE concat('dependency closure invalidated by ', $1::text)
                   END,
                   updated_at = NOW()
              FROM invalid i
             WHERE c.id = i.node_id
               AND c.validity_status NOT IN ('superseded', 'recomputing')
            """,
            context_id,
            reason,
        )
        await db.execute(
            """
            WITH RECURSIVE invalid(node_id) AS (
              SELECT $1::uuid
              UNION
              SELECT edge.node_id
                FROM invalid i
                JOIN LATERAL (
                  SELECT d.dependent_id AS node_id
                    FROM dependencies d
                   WHERE d.dependency_id = i.node_id
                  UNION
                  SELECT r.context_id AS node_id
                    FROM context_relations r
                   WHERE r.related_context_id = i.node_id
                     AND r.relation_type IN (
                       'alias_of', 'duplicate_of', 'materialized_from'
                     )
                ) edge ON TRUE
            )
            INSERT INTO context_invalidations (
              context_id, cause_event_id, source_context_id, source_version,
              reason_hash
            )
            SELECT i.node_id, COALESCE($2::uuid, $3::uuid), $1, $5,
                   encode(digest($4, 'sha256'), 'hex')
              FROM invalid i
            ON CONFLICT (context_id, cause_event_id) DO NOTHING
            """,
            context_id,
            source_event.get("event_id") if source_event else None,
            event_id,
            reason,
            source_event.get("source_version") if source_event else row["version"],
        )
        await db.execute(
            """
            INSERT INTO propagation_trace (event_id, trace_type, payload)
            VALUES (
              $1, 'stale_closure',
              jsonb_build_object(
                'source_context_id', $2::uuid::text,
                'reason_hash', encode(digest($3, 'sha256'), 'hex')
              )
            )
            """,
            event_id,
            context_id,
            reason,
        )
        if transitioned:
            await self._log_transition(
                db,
                actor=ctx.agent_id,
                uri=row["uri"],
                from_status="active",
                to_status="stale",
                reason=reason,
            )

    async def recover_from_stale(
        self,
        db: ScopedRepo,
        context_id: UUID,
        ctx: RequestContext,
    ) -> None:
        row = await self._fetch_context_row(db, context_id)
        if row["status"] != "stale":
            return
        dependency_block = await db.fetchval(
            """
            SELECT 1
              FROM context_invalidations i
              JOIN change_events e ON e.event_id = i.cause_event_id
             WHERE i.context_id = $1
               AND i.resolved_at IS NULL
               AND (
                 e.change_type != 'marked_stale'
                 OR e.parent_event_id IS NOT NULL
               )
             LIMIT 1
            """,
            context_id,
        )
        if dependency_block is not None:
            raise ConflictError(
                "Context is stale because a dependency changed; recompute it before reading"
            )

        result = await db.execute(
            """
            UPDATE contexts
            SET status = 'active',
                validity_status = 'fresh',
                validity_reason = NULL,
                stale_at = NULL,
                last_accessed_at = NOW(),
                updated_at = NOW()
            WHERE id = $1 AND status = 'stale'
            """,
            context_id,
        )
        if result == "UPDATE 0":
            return
        await db.execute(
            """
            UPDATE context_invalidations i
               SET resolved_at = NOW()
              FROM change_events e
             WHERE i.context_id = $1
               AND i.cause_event_id = e.event_id
               AND i.resolved_at IS NULL
               AND e.parent_event_id IS NULL
            """,
            context_id,
        )

        await self._log_transition(
            db,
            actor=ctx.agent_id,
            uri=row["uri"],
            from_status="stale",
            to_status="active",
            reason="read_access",
        )

    async def mark_archived(
        self,
        db: ScopedRepo,
        context_id: UUID,
        ctx: RequestContext,
    ) -> None:
        row = await self._fetch_context_row(db, context_id)
        if row["status"] != "stale":
            return

        result = await db.execute(
            """
            UPDATE contexts
            SET status = 'archived',
                archived_at = NOW(),
                updated_at = NOW(),
                l0_embedding = NULL
            WHERE id = $1 AND status = 'stale'
            """,
            context_id,
        )
        if result == "UPDATE 0":
            return

        await self._log_transition(
            db,
            actor=ctx.agent_id,
            uri=row["uri"],
            from_status="stale",
            to_status="archived",
            reason="archive_policy",
        )

    async def recover_from_archived(
        self,
        db: ScopedRepo,
        context_id: UUID,
        ctx: RequestContext,
    ) -> None:
        row = await self._fetch_context_row(db, context_id, extra_columns=("l0_content",))
        if row["status"] != "archived":
            return

        result = await db.execute(
            """
            UPDATE contexts
            SET status = 'active',
                archived_at = NULL,
                updated_at = NOW()
            WHERE id = $1 AND status = 'archived'
            """,
            context_id,
        )
        if result == "UPDATE 0":
            return

        if row["l0_content"]:
            if self._indexer is None:
                raise RuntimeError(
                    "LifecycleService requires IndexerService to recover archived contexts with embeddings"
                )
            success = await self._indexer.update_embedding(db, context_id, row["l0_content"])
            if not success:
                raise RuntimeError(
                    f"Failed to restore embedding for archived context {context_id}"
                )

        await self._log_transition(
            db,
            actor=ctx.agent_id,
            uri=row["uri"],
            from_status="archived",
            to_status="active",
            reason="restore_archive",
        )

    async def mark_deleted(
        self,
        db: ScopedRepo,
        context_id: UUID,
        ctx: RequestContext,
    ) -> None:
        row = await self._fetch_context_row(db, context_id)
        if row["status"] != "archived":
            return

        result = await db.execute(
            """
            UPDATE contexts
            SET status = 'deleted',
                deleted_at = NOW(),
                updated_at = NOW()
            WHERE id = $1 AND status = 'archived'
            """,
            context_id,
        )
        if result == "UPDATE 0":
            return

        await self._log_transition(
            db,
            actor=ctx.agent_id,
            uri=row["uri"],
            from_status="archived",
            to_status="deleted",
            reason="delete_policy",
        )

    async def upsert_policy(
        self,
        db: ScopedRepo,
        context_type: str | StrEnum,
        scope: str | StrEnum,
        stale_after_days: int,
        archive_after_days: int,
        delete_after_days: int,
        ctx: RequestContext,
    ) -> LifecyclePolicy:
        normalized_context_type = self._normalize_enum_value(context_type)
        normalized_scope = self._normalize_enum_value(scope)
        row = await db.fetchrow(
            """
            INSERT INTO lifecycle_policies (
                context_type, scope,
                stale_after_days, archive_after_days, delete_after_days,
                account_id, updated_at
            )
            VALUES (
                $1, $2,
                $3, $4, $5,
                current_setting('app.account_id'), NOW()
            )
            ON CONFLICT (account_id, context_type, scope)
            DO UPDATE SET
                stale_after_days = EXCLUDED.stale_after_days,
                archive_after_days = EXCLUDED.archive_after_days,
                delete_after_days = EXCLUDED.delete_after_days,
                updated_at = NOW()
            RETURNING *
            """,
            normalized_context_type,
            normalized_scope,
            stale_after_days,
            archive_after_days,
            delete_after_days,
        )
        policy = self._row_to_policy(row)

        if self._audit:
            await self._audit.log_strict(
                db,
                ctx.agent_id,
                "policy_change",
                None,
                "success",
                metadata={
                    "operation": "upsert_lifecycle_policy",
                    "context_type": policy.context_type,
                    "scope": policy.scope,
                    "stale_after_days": policy.stale_after_days,
                    "archive_after_days": policy.archive_after_days,
                    "delete_after_days": policy.delete_after_days,
                },
            )
        return policy

    async def ensure_default_policies(
        self,
        db: ScopedRepo,
        ctx: RequestContext,
    ) -> None:
        for context_type, scope, stale_days, archive_days, delete_days in DEFAULT_LIFECYCLE_POLICIES:
            row = await db.fetchrow(
                """
                INSERT INTO lifecycle_policies (
                    context_type, scope,
                    stale_after_days, archive_after_days, delete_after_days,
                    account_id, updated_at
                )
                VALUES (
                    $1, $2,
                    $3, $4, $5,
                    current_setting('app.account_id'), NOW()
                )
                ON CONFLICT (account_id, context_type, scope) DO NOTHING
                RETURNING *
                """,
                context_type,
                scope,
                stale_days,
                archive_days,
                delete_days,
            )
            if row is None or self._audit is None:
                continue

            policy = self._row_to_policy(row)
            await self._audit.log_strict(
                db,
                ctx.agent_id,
                "policy_change",
                None,
                "success",
                metadata={
                    "operation": "seed_default_lifecycle_policy",
                    "seeded_by": ctx.agent_id,
                    "context_type": policy.context_type,
                    "scope": policy.scope,
                    "stale_after_days": policy.stale_after_days,
                    "archive_after_days": policy.archive_after_days,
                    "delete_after_days": policy.delete_after_days,
                },
            )

    @staticmethod
    def _normalize_enum_value(value: str | StrEnum) -> str:
        if isinstance(value, StrEnum):
            return value.value
        return str(value)

    @staticmethod
    def _row_to_policy(row) -> LifecyclePolicy:
        return LifecyclePolicy(
            context_type=row["context_type"],
            scope=row["scope"],
            stale_after_days=row["stale_after_days"],
            archive_after_days=row["archive_after_days"],
            delete_after_days=row["delete_after_days"],
            account_id=row["account_id"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    async def _fetch_context_row(
        db: ScopedRepo,
        context_id: UUID,
        extra_columns: tuple[str, ...] = (),
    ):
        columns = ["id", "uri", "status", *extra_columns]
        row = await db.fetchrow(
            f"""
            SELECT {", ".join(columns)}
            FROM contexts
            WHERE id = $1 AND status != 'deleted'
            """,
            context_id,
        )
        if row is None:
            raise NotFoundError(f"Context {context_id} not found")
        return row

    async def _log_transition(
        self,
        db: ScopedRepo,
        *,
        actor: str,
        uri: str,
        from_status: str,
        to_status: str,
        reason: str | None = None,
    ) -> None:
        if self._audit is None:
            return

        metadata = {
            "from_status": from_status,
            "to_status": to_status,
        }
        if reason is not None:
            metadata["reason"] = reason

        await self._audit.log_strict(
            db,
            actor,
            "lifecycle_transition",
            uri,
            "success",
            metadata=metadata,
        )
