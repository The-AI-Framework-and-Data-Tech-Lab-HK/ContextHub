"""MVP ACL: default visibility and write permission checks."""

from contexthub.db.repository import ScopedRepo
from contexthub.models.context import Scope
from contexthub.models.request import RequestContext


class ACLService:
    """MVP 默认可见性 / 默认写权限。"""

    async def check_read(self, db: ScopedRepo, uri: str, ctx: RequestContext) -> bool:
        row = await db.fetchrow(
            "SELECT scope, owner_space FROM contexts WHERE uri = $1 AND status != 'deleted'",
            uri,
        )
        if row is None:
            return False
        return await self._can_read(db, row["scope"], row["owner_space"], ctx)

    async def check_write(self, db: ScopedRepo, uri: str, ctx: RequestContext) -> bool:
        row = await db.fetchrow(
            "SELECT scope, owner_space FROM contexts WHERE uri = $1 AND status != 'deleted'",
            uri,
        )
        if row is None:
            return False
        return await self._can_write(db, row["scope"], row["owner_space"], ctx)

    async def check_write_target(
        self,
        db: ScopedRepo,
        scope: Scope,
        owner_space: str | None,
        ctx: RequestContext,
    ) -> bool:
        return await self._can_write(db, scope, owner_space, ctx)

    async def get_visible_team_paths(self, db: ScopedRepo, agent_id: str) -> list[str]:
        rows = await db.fetch(
            """
            WITH RECURSIVE visible_teams AS (
                SELECT t.id, t.path, t.parent_id
                FROM teams t JOIN team_memberships tm ON t.id = tm.team_id
                WHERE tm.agent_id = $1
                UNION ALL
                SELECT t.id, t.path, t.parent_id
                FROM teams t JOIN visible_teams vt ON t.id = vt.parent_id
            )
            SELECT DISTINCT path FROM visible_teams
            """,
            agent_id,
        )
        return [r["path"] for r in rows]

    async def filter_visible(
        self, db: ScopedRepo, contexts: list, ctx: RequestContext
    ) -> list:
        visible_paths = await self.get_visible_team_paths(db, ctx.agent_id)
        result = []
        for c in contexts:
            scope = self._get_value(c, "scope")
            owner_space = self._get_value(c, "owner_space")
            status = self._get_value(c, "status")
            if status == "deleted":
                continue
            if scope == Scope.USER:
                continue
            if scope == Scope.DATALAKE:
                result.append(c)
            elif scope == Scope.AGENT and owner_space == ctx.agent_id:
                result.append(c)
            elif scope == Scope.TEAM and owner_space in visible_paths:
                result.append(c)
        return result

    # ---- internal helpers ----

    async def _can_read(
        self, db: ScopedRepo, scope: str, owner_space: str | None, ctx: RequestContext
    ) -> bool:
        if scope == Scope.USER:
            return False
        if scope == Scope.DATALAKE:
            return True
        if scope == Scope.AGENT:
            return owner_space == ctx.agent_id
        if scope == Scope.TEAM:
            visible = await self.get_visible_team_paths(db, ctx.agent_id)
            return owner_space in visible
        return False

    async def _can_write(
        self, db: ScopedRepo, scope: str, owner_space: str | None, ctx: RequestContext
    ) -> bool:
        if scope == Scope.USER:
            return False
        if scope == Scope.AGENT:
            return owner_space == ctx.agent_id
        if scope == Scope.DATALAKE:
            return ctx.agent_id in {"system", "catalog_sync"}
        if scope == Scope.TEAM:
            visible = await self.get_visible_team_paths(db, ctx.agent_id)
            if owner_space not in visible:
                return False
            # check read_write access on the direct team
            has_rw = await db.fetchval(
                """
                SELECT 1 FROM team_memberships tm
                JOIN teams t ON t.id = tm.team_id
                WHERE tm.agent_id = $1 AND t.path = $2 AND tm.access = 'read_write'
                """,
                ctx.agent_id,
                owner_space,
            )
            return has_rw is not None
        return False

    @staticmethod
    def _get_value(item, key: str):
        try:
            return item[key]
        except (KeyError, TypeError, IndexError):
            return getattr(item, key)
