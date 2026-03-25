"""SkillService: publish, subscribe, resolve skill versions."""

from __future__ import annotations

import json
from uuid import UUID

from contexthub.db.repository import ScopedRepo
from contexthub.errors import BadRequestError, ForbiddenError, NotFoundError
from contexthub.models.request import RequestContext
from contexthub.models.skill import (
    SkillContent,
    SkillSubscription,
    SkillVersion,
    SkillVersionStatus,
)
from contexthub.services.acl_service import ACLService
from contexthub.services.indexer_service import IndexerService


class SkillService:
    def __init__(self, indexer: IndexerService, acl: ACLService):
        self._indexer = indexer
        self._acl = acl

    async def publish_version(
        self, db: ScopedRepo, skill_uri: str, content: str,
        changelog: str | None, is_breaking: bool, ctx: RequestContext,
    ) -> SkillVersion:
        # 1. Check skill exists
        skill = await db.fetchrow(
            "SELECT id, context_type FROM contexts WHERE uri = $1 AND status != 'deleted'",
            skill_uri,
        )
        if skill is None:
            raise NotFoundError(f"Skill {skill_uri} not found")
        if skill["context_type"] != "skill":
            raise BadRequestError("Context is not a skill")

        # 2. ACL write check
        if not await self._acl.check_write(db, skill_uri, ctx):
            raise ForbiddenError()

        skill_id = skill["id"]

        # 3. Lock the contexts row to prevent concurrent publish
        await db.fetchrow(
            "SELECT id FROM contexts WHERE id = $1 FOR UPDATE",
            skill_id,
        )

        # 4. Determine new version
        max_ver = await db.fetchval(
            "SELECT COALESCE(MAX(version), 0) FROM skill_versions WHERE skill_id = $1",
            skill_id,
        )
        new_version = max_ver + 1

        # 5. Insert skill_versions
        await db.execute(
            """
            INSERT INTO skill_versions
                (skill_id, version, content, changelog, is_breaking, status, published_by, published_at)
            VALUES ($1, $2, $3, $4, $5, 'published', $6, NOW())
            """,
            skill_id, new_version, content, changelog, is_breaking, ctx.agent_id,
        )

        # 6. Generate L0/L1
        generated = await self._indexer.generate("skill", content)

        # 7. Update contexts head pointer
        await db.execute(
            """
            UPDATE contexts
            SET l0_content = $1, l1_content = $2, l2_content = $3,
                version = $4, updated_at = NOW()
            WHERE id = $5
            """,
            generated.l0, generated.l1, content, new_version, skill_id,
        )

        # 8. Change event
        meta = json.dumps({"is_breaking": is_breaking, "changelog": changelog})
        await db.execute(
            """
            INSERT INTO change_events
                (context_id, account_id, change_type, actor, new_version, metadata)
            VALUES ($1, current_setting('app.account_id'), 'version_published', $2, $3, $4)
            """,
            skill_id, ctx.agent_id, str(new_version), meta,
        )

        # Embedding consistency for skill head
        if generated.l0:
            await self._indexer.update_embedding(db, skill_id, generated.l0)

        return SkillVersion(
            skill_id=skill_id,
            version=new_version,
            content=content,
            changelog=changelog,
            is_breaking=is_breaking,
            status=SkillVersionStatus.PUBLISHED,
            published_by=ctx.agent_id,
        )

    async def get_versions(
        self, db: ScopedRepo, skill_uri: str, ctx: RequestContext,
    ) -> list[SkillVersion]:
        skill = await db.fetchrow(
            "SELECT id, context_type FROM contexts WHERE uri = $1 AND status != 'deleted'",
            skill_uri,
        )
        if skill is None:
            raise NotFoundError(f"Skill {skill_uri} not found")
        if skill["context_type"] != "skill":
            raise BadRequestError("Context is not a skill")

        if not await self._acl.check_read(db, skill_uri, ctx):
            raise ForbiddenError()

        rows = await db.fetch(
            """
            SELECT skill_id, version, content, changelog, is_breaking, status,
                   published_by, published_at
            FROM skill_versions
            WHERE skill_id = $1 AND status IN ('published', 'deprecated')
            ORDER BY version DESC
            """,
            skill["id"],
        )
        return [
            SkillVersion(
                skill_id=r["skill_id"],
                version=r["version"],
                content=r["content"],
                changelog=r["changelog"],
                is_breaking=r["is_breaking"],
                status=r["status"],
                published_by=r["published_by"],
                published_at=r["published_at"],
            )
            for r in rows
        ]

    async def subscribe(
        self, db: ScopedRepo, skill_uri: str, pinned_version: int | None,
        ctx: RequestContext,
    ) -> SkillSubscription:
        skill = await db.fetchrow(
            "SELECT id, context_type FROM contexts WHERE uri = $1 AND status != 'deleted'",
            skill_uri,
        )
        if skill is None:
            raise NotFoundError(f"Skill {skill_uri} not found")
        if skill["context_type"] != "skill":
            raise BadRequestError("Context is not a skill")

        if not await self._acl.check_read(db, skill_uri, ctx):
            raise ForbiddenError()

        skill_id = skill["id"]

        if pinned_version is not None:
            ver = await db.fetchrow(
                """
                SELECT status FROM skill_versions
                WHERE skill_id = $1 AND version = $2
                """,
                skill_id, pinned_version,
            )
            if ver is None:
                raise BadRequestError(f"Version {pinned_version} does not exist")
            if ver["status"] != "published":
                raise BadRequestError(f"Version {pinned_version} is not published")

        row = await db.fetchrow(
            """
            INSERT INTO skill_subscriptions (agent_id, skill_id, pinned_version, account_id)
            VALUES ($1, $2, $3, current_setting('app.account_id'))
            ON CONFLICT (agent_id, skill_id)
            DO UPDATE SET pinned_version = EXCLUDED.pinned_version
            RETURNING *
            """,
            ctx.agent_id, skill_id, pinned_version,
        )
        return SkillSubscription(
            id=row["id"],
            agent_id=row["agent_id"],
            skill_id=row["skill_id"],
            pinned_version=row["pinned_version"],
            account_id=row["account_id"],
            created_at=row["created_at"],
        )

    async def read_resolved(
        self,
        db: ScopedRepo,
        skill_id: UUID,
        agent_id: str,
        requested_version: int | None = None,
    ) -> SkillContent:
        if requested_version is not None:
            return await self._read_version(db, skill_id, requested_version)

        # Check subscription
        sub = await db.fetchrow(
            "SELECT pinned_version FROM skill_subscriptions WHERE agent_id = $1 AND skill_id = $2",
            agent_id, skill_id,
        )

        if sub is not None and sub["pinned_version"] is not None:
            pinned = sub["pinned_version"]
            content = await self._read_version(db, skill_id, pinned)
            # Check if newer version exists
            latest_ver = await db.fetchval(
                """
                SELECT MAX(version) FROM skill_versions
                WHERE skill_id = $1 AND status = 'published'
                """,
                skill_id,
            )
            if latest_ver and latest_ver > pinned:
                content.advisory = f"v{latest_ver} available, currently pinned to v{pinned}"
            return content

        # Floating or no subscription: return latest
        return await self._read_latest(db, skill_id)

    async def _read_version(
        self, db: ScopedRepo, skill_id: UUID, version: int,
    ) -> SkillContent:
        row = await db.fetchrow(
            """
            SELECT content, version, status FROM skill_versions
            WHERE skill_id = $1 AND version = $2 AND status IN ('published', 'deprecated')
            """,
            skill_id, version,
        )
        if row is None:
            raise NotFoundError(f"Version {version} not found or not accessible")

        advisory = None
        if row["status"] == "deprecated":
            advisory = f"v{version} is deprecated"

        return SkillContent(
            content=row["content"],
            version=row["version"],
            status=row["status"],
            advisory=advisory,
        )

    async def _read_latest(
        self, db: ScopedRepo, skill_id: UUID,
    ) -> SkillContent:
        # Use contexts head pointer, but verify at least one published version exists
        ctx_row = await db.fetchrow(
            "SELECT l2_content, version FROM contexts WHERE id = $1",
            skill_id,
        )
        if ctx_row is None:
            raise NotFoundError("Skill not found")

        has_published = await db.fetchval(
            "SELECT 1 FROM skill_versions WHERE skill_id = $1 AND status = 'published' LIMIT 1",
            skill_id,
        )
        if not has_published:
            raise NotFoundError("No published version exists for this skill")

        return SkillContent(
            content=ctx_row["l2_content"],
            version=ctx_row["version"],
            status=SkillVersionStatus.PUBLISHED,
        )
