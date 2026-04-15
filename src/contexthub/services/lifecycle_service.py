"""Lifecycle service: policy management and context status transitions."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from contexthub.db.repository import ScopedRepo
from contexthub.errors import NotFoundError
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
    ) -> None:
        row = await self._fetch_context_row(db, context_id)
        if row["status"] != "active":
            return

        result = await db.execute(
            """
            UPDATE contexts
            SET status = 'stale',
                stale_at = NOW(),
                updated_at = NOW()
            WHERE id = $1 AND status = 'active'
            """,
            context_id,
        )
        if result == "UPDATE 0":
            return

        await db.execute(
            """
            INSERT INTO change_events
                (context_id, account_id, change_type, actor, diff_summary)
            VALUES ($1, $2, 'marked_stale', $3, $4)
            """,
            context_id,
            ctx.account_id,
            ctx.agent_id,
            reason,
        )
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

        result = await db.execute(
            """
            UPDATE contexts
            SET status = 'active',
                stale_at = NULL,
                last_accessed_at = NOW(),
                updated_at = NOW()
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
