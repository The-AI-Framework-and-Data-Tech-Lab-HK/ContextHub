"""ContextService: CRUD orchestration over ContextStore + ACLService."""

from __future__ import annotations

from contexthub.db.repository import ScopedRepo
from contexthub.errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PreconditionRequiredError,
    UnsupportedScopeError,
)
from contexthub.models.context import (
    Context,
    ContextStatus,
    CreateContextRequest,
    Scope,
    UpdateContextRequest,
)
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.indexer_service import IndexerService
from contexthub.store.context_store import ContextStore


class ContextService:
    def __init__(
        self, store: ContextStore, acl: ACLService,
        indexer: IndexerService | None = None,
        audit: AuditService | None = None,
    ):
        self._store = store
        self._acl = acl
        self._indexer = indexer
        self._audit = audit

    # ---- create ----

    async def create(
        self, db: ScopedRepo, body: CreateContextRequest, ctx: RequestContext
    ) -> Context:
        self._validate_uri_scope(body)

        if not await self._acl.check_write_target(db, body.scope, body.owner_space, ctx):
            raise ForbiddenError()

        try:
            row = await db.fetchrow(
                """
                INSERT INTO contexts
                    (uri, context_type, scope, owner_space, account_id,
                     l0_content, l1_content, l2_content, file_path, tags)
                VALUES ($1,$2,$3,$4, current_setting('app.account_id'),
                        $5,$6,$7,$8,$9)
                RETURNING *
                """,
                body.uri,
                body.context_type.value,
                body.scope.value,
                body.owner_space,
                body.l0_content,
                body.l1_content,
                body.l2_content,
                body.file_path,
                body.tags,
            )
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise ConflictError(f"Context {body.uri} already exists")
            raise

        await db.execute(
            """
            INSERT INTO change_events (context_id, account_id, change_type, actor)
            VALUES ($1, current_setting('app.account_id'), 'created', $2)
            """,
            row["id"],
            ctx.agent_id,
        )

        # Embedding consistency: new active context with l0_content
        if self._indexer and body.l0_content:
            await self._indexer.update_embedding(db, row["id"], body.l0_content)

        result = self._row_to_context(row)

        if self._audit:
            await self._audit.log_strict(
                db, ctx.agent_id, "create", body.uri, "success",
                metadata={"context_type": body.context_type.value, "scope": body.scope.value},
            )
        return result

    async def update(
        self, db: ScopedRepo, uri: str, body: UpdateContextRequest, ctx: RequestContext
    ) -> Context:
        if not await self._acl.check_write(db, uri, ctx):
            await self._raise_for_missing_or_forbidden(db, uri)

        if ctx.expected_version is None:
            raise PreconditionRequiredError()

        if body.status == ContextStatus.DELETED:
            raise BadRequestError("Use DELETE endpoint to delete a context")

        sets: list[str] = []
        args: list = []
        idx = 1

        content_changed = False
        for field in ("l0_content", "l1_content", "l2_content", "file_path"):
            val = getattr(body, field)
            if val is not None:
                sets.append(f"{field} = ${idx}")
                args.append(val)
                idx += 1
                content_changed = True

        if body.tags is not None:
            sets.append(f"tags = ${idx}")
            args.append(body.tags)
            idx += 1

        if content_changed:
            sets.extend(["status = 'active'", "stale_at = NULL", "archived_at = NULL"])
        elif body.status is not None:
            sets.append(f"status = ${idx}")
            args.append(body.status.value)
            idx += 1
            if body.status == ContextStatus.STALE:
                sets.append("stale_at = NOW()")
            elif body.status == ContextStatus.ARCHIVED:
                sets.append("archived_at = NOW()")
            elif body.status == ContextStatus.ACTIVE:
                sets.extend(["stale_at = NULL", "archived_at = NULL"])

        if not sets:
            raise BadRequestError("No fields to update")

        sets.extend(["version = version + 1", "updated_at = NOW()"])
        set_clause = ", ".join(sets)

        args.append(uri)
        uri_idx = idx
        idx += 1
        args.append(ctx.expected_version)
        ver_idx = idx

        row = await db.fetchrow(
            f"""
            UPDATE contexts SET {set_clause}
            WHERE uri = ${uri_idx} AND version = ${ver_idx} AND status != 'deleted'
            RETURNING *
            """,
            *args,
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

        # Embedding consistency after update
        if self._indexer:
            new_status = row["status"]
            if new_status == "archived":
                await self._indexer.clear_embedding(db, row["id"])
            elif row["l0_content"] and new_status in ("active", "stale"):
                # Re-embed if content changed or status restored to active
                if content_changed or (body.status == ContextStatus.ACTIVE):
                    await self._indexer.update_embedding(db, row["id"], row["l0_content"])

        result = self._row_to_context(row)

        if self._audit:
            changed = [f for f in ("l0_content", "l1_content", "l2_content", "file_path", "tags", "status")
                        if getattr(body, f, None) is not None]
            await self._audit.log_strict(
                db, ctx.agent_id, "update", uri, "success",
                metadata={"changed_fields": changed},
            )
        return result

    # ---- delete ----

    async def delete(
        self, db: ScopedRepo, uri: str, ctx: RequestContext
    ) -> None:
        if not await self._acl.check_write(db, uri, ctx):
            await self._raise_for_missing_or_forbidden(db, uri)

        if ctx.expected_version is None:
            raise PreconditionRequiredError()

        row = await db.fetchrow(
            """
            UPDATE contexts
            SET status = 'deleted', deleted_at = NOW(), version = version + 1, updated_at = NOW()
            WHERE uri = $1 AND version = $2 AND status != 'deleted'
            RETURNING id
            """,
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
            VALUES ($1, current_setting('app.account_id'), 'deleted', $2)
            """,
            row["id"],
            ctx.agent_id,
        )

        if self._audit:
            await self._audit.log_strict(
                db, ctx.agent_id, "delete", uri, "success",
                metadata={"previous_version": ctx.expected_version},
            )

    # ---- get_dependencies ----

    async def get_dependencies(
        self, db: ScopedRepo, uri: str, ctx: RequestContext
    ) -> list[dict]:
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

        context_id = await db.fetchval(
            "SELECT id FROM contexts WHERE uri = $1 AND status != 'deleted'", uri
        )
        if context_id is None:
            raise NotFoundError(f"Context {uri} not found")

        rows = await db.fetch(
            """
            SELECT d.dep_type, d.pinned_version,
                   c1.uri AS dependent_uri, c2.uri AS dependency_uri
            FROM dependencies d
            JOIN contexts c1 ON c1.id = d.dependent_id
            JOIN contexts c2 ON c2.id = d.dependency_id
            WHERE d.dependent_id = $1 OR d.dependency_id = $1
            """,
            context_id,
        )
        result = [dict(r) for r in rows]

        if self._audit:
            await self._audit.log_best_effort(
                db, ctx.agent_id, "read", uri, "success",
                metadata={"sub_action": "get_dependencies", "result_count": len(result)},
            )
        return result

    # ---- helpers ----

    @staticmethod
    def _validate_uri_scope(body: CreateContextRequest) -> None:
        if body.scope == Scope.USER:
            raise UnsupportedScopeError()
        if body.scope == Scope.DATALAKE:
            if not body.uri.startswith("ctx://datalake/"):
                raise BadRequestError("datalake URI must start with ctx://datalake/")
            if body.owner_space is not None:
                raise BadRequestError("datalake scope must have owner_space=None")
        elif body.scope == Scope.TEAM:
            if not body.uri.startswith("ctx://team/"):
                raise BadRequestError("team URI must start with ctx://team/")
            if body.owner_space is None:
                raise BadRequestError("team scope requires owner_space; use '' for root team")
            if body.owner_space:
                expected = f"ctx://team/{body.owner_space}/"
                if not (body.uri.startswith(expected) or body.uri == expected.rstrip("/")):
                    raise BadRequestError(
                        f"team URI must start with {expected} for owner_space={body.owner_space}"
                    )
        elif body.scope == Scope.AGENT:
            if not body.owner_space:
                raise BadRequestError("agent scope requires owner_space")
            expected = f"ctx://agent/{body.owner_space}/"
            if not (body.uri.startswith(expected) or body.uri == expected.rstrip("/")):
                raise BadRequestError(
                    f"agent URI must start with {expected} for owner_space={body.owner_space}"
                )

    @staticmethod
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

    @staticmethod
    async def _raise_for_missing_or_forbidden(db: ScopedRepo, uri: str) -> None:
        exists = await db.fetchval(
            "SELECT 1 FROM contexts WHERE uri = $1 AND status != 'deleted'",
            uri,
        )
        if exists is None:
            raise NotFoundError(f"Context {uri} not found")
        raise ForbiddenError()
