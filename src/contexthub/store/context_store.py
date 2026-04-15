"""ContextStore: read / write / ls / stat on the contexts table."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from contexthub.db.repository import ScopedRepo
from contexthub.errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PreconditionRequiredError,
)
from contexthub.models.context import ContextLevel
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.lifecycle_service import LifecycleService
from contexthub.services.masking_service import MaskingService

LEVEL_COLUMNS = {
    ContextLevel.L0: "l0_content",
    ContextLevel.L1: "l1_content",
    ContextLevel.L2: "l2_content",
}


@dataclass
class ContextStat:
    id: UUID
    uri: str
    context_type: str
    scope: str
    owner_space: str | None
    status: str
    version: int
    tags: list[str]
    active_count: int
    adopted_count: int
    ignored_count: int
    created_at: datetime
    updated_at: datetime
    last_accessed_at: datetime


class ContextStore:
    def __init__(
        self,
        acl: ACLService,
        masking: MaskingService,
        audit: AuditService | None = None,
        lifecycle: LifecycleService | None = None,
    ):
        self._acl = acl
        self._masking = masking
        self._audit = audit
        self._lifecycle = lifecycle

    async def read(
        self, db: ScopedRepo, uri: str, level: ContextLevel, ctx: RequestContext
    ) -> str:
        if uri.startswith("ctx://user/"):
            raise BadRequestError("scope=user is not supported in Task 2 public API")

        decision = await self._acl.check_read_access(db, uri, ctx)
        if not decision.allowed:
            exists = await db.fetchval(
                "SELECT 1 FROM contexts WHERE uri = $1 AND status != 'deleted'", uri,
            )
            if exists is None:
                raise NotFoundError(f"Context {uri} not found")
            if self._audit and decision.reason in ("explicit deny", "parent team deny"):
                await self._audit.log_access_denied(
                    ctx.account_id, ctx.agent_id, uri,
                    metadata={"action": "read", "reason": decision.reason},
                )
            raise ForbiddenError()

        col = LEVEL_COLUMNS[level]
        row = await db.fetchrow(
            f"""
            SELECT id, status, {col}
            FROM contexts
            WHERE uri = $1 AND status != 'deleted'
            """,
            uri,
        )
        if row is None:
            raise NotFoundError(f"Context {uri} not found")

        if row["status"] == "stale" and self._lifecycle is not None:
            await self._lifecycle.recover_from_stale(db, row["id"], ctx)
        else:
            await db.execute(
                "UPDATE contexts SET last_accessed_at = NOW() WHERE uri = $1", uri
            )

        content = row[col] or ""
        if decision.field_masks:
            content = self._masking.apply_masks(content, decision.field_masks)

        if self._audit:
            await self._audit.log_best_effort(
                db, ctx.agent_id, "read", uri, "success",
                metadata={"level": level.value},
            )
        return content

    async def write(
        self,
        db: ScopedRepo,
        uri: str,
        level: ContextLevel,
        content: str,
        ctx: RequestContext,
    ) -> int:
        if not await self._acl.check_write(db, uri, ctx):
            await self._raise_for_missing_or_forbidden(db, uri)

        if ctx.expected_version is None:
            raise PreconditionRequiredError()

        col = LEVEL_COLUMNS[level]
        row = await db.fetchrow(
            f"""
            UPDATE contexts
            SET {col} = $1, status = 'active', stale_at = NULL, archived_at = NULL,
                version = version + 1, updated_at = NOW()
            WHERE uri = $2 AND version = $3 AND status != 'deleted'
            RETURNING id, version
            """,
            content,
            uri,
            ctx.expected_version,
        )
        if row is None:
            exists = await db.fetchval(
                "SELECT 1 FROM contexts WHERE uri = $1 AND status != 'deleted'", uri
            )
            if exists:
                raise ConflictError("Version mismatch")
            raise NotFoundError(f"Context {uri} not found")

        await db.execute(
            """
            INSERT INTO change_events (context_id, account_id, change_type, actor)
            VALUES ($1, current_setting('app.account_id'), 'modified', $2)
            """,
            row["id"],
            ctx.agent_id,
        )

        if self._audit:
            await self._audit.log_strict(
                db, ctx.agent_id, "write", uri, "success",
                metadata={"level": level.value, "new_version": row["version"]},
            )
        return row["version"]

    async def ls(
        self, db: ScopedRepo, path: str, ctx: RequestContext
    ) -> list[str]:
        if path.startswith("ctx://user/"):
            raise BadRequestError("scope=user is not supported in Task 2 public API")

        prefix = path.rstrip("/") + "/"
        rows = await db.fetch(
            """
            SELECT uri, scope, owner_space, status
            FROM contexts WHERE uri LIKE $1 AND status != 'deleted'
            """,
            prefix + "%",
        )
        visible_with_masks = await self._acl.filter_visible_with_acl(db, rows, ctx)

        children: set[str] = set()
        prefix_len = len(prefix)
        for r, _masks in visible_with_masks:
            uri = self._get_value(r, "uri")
            remainder = uri[prefix_len:]
            child = remainder.split("/", 1)[0]
            if child:
                children.add(child)

        result = sorted(children)

        if self._audit:
            await self._audit.log_best_effort(
                db, ctx.agent_id, "ls", path, "success",
                metadata={"result_count": len(result)},
            )
        return result

    async def stat(
        self, db: ScopedRepo, uri: str, ctx: RequestContext
    ) -> ContextStat:
        if uri.startswith("ctx://user/"):
            raise BadRequestError("scope=user is not supported in Task 2 public API")

        decision = await self._acl.check_read_access(db, uri, ctx)
        if not decision.allowed:
            exists = await db.fetchval(
                "SELECT 1 FROM contexts WHERE uri = $1 AND status != 'deleted'", uri,
            )
            if exists is None:
                raise NotFoundError(f"Context {uri} not found")
            if self._audit and decision.reason in ("explicit deny", "parent team deny"):
                await self._audit.log_access_denied(
                    ctx.account_id, ctx.agent_id, uri,
                    metadata={"action": "stat", "reason": decision.reason},
                )
            raise ForbiddenError()

        row = await db.fetchrow(
            """
            SELECT id, uri, context_type, scope, owner_space, status, version,
                   tags, active_count, adopted_count, ignored_count,
                   created_at, updated_at, last_accessed_at
            FROM contexts WHERE uri = $1 AND status != 'deleted'
            """,
            uri,
        )
        if row is None:
            raise NotFoundError(f"Context {uri} not found")

        stat_result = ContextStat(
            id=row["id"],
            uri=row["uri"],
            context_type=row["context_type"],
            scope=row["scope"],
            owner_space=row["owner_space"],
            status=row["status"],
            version=row["version"],
            tags=list(row["tags"] or []),
            active_count=row["active_count"],
            adopted_count=row["adopted_count"],
            ignored_count=row["ignored_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_accessed_at=row["last_accessed_at"],
        )

        if self._audit:
            await self._audit.log_best_effort(
                db, ctx.agent_id, "stat", uri, "success",
            )
        return stat_result

    @staticmethod
    async def _raise_for_missing_or_forbidden(db: ScopedRepo, uri: str) -> None:
        exists = await db.fetchval(
            "SELECT 1 FROM contexts WHERE uri = $1 AND status != 'deleted'",
            uri,
        )
        if exists is None:
            raise NotFoundError(f"Context {uri} not found")
        raise ForbiddenError()

    @staticmethod
    def _get_value(item, key: str):
        try:
            return item[key]
        except (KeyError, TypeError, IndexError):
            return getattr(item, key)
