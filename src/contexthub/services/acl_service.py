"""MVP ACL: default visibility and write permission checks."""

from __future__ import annotations

from contexthub.db.repository import ScopedRepo
from contexthub.models.access import AccessPolicy, PolicyEffect
from contexthub.models.context import Scope
from contexthub.models.request import RequestContext
from contexthub.services.access_decision import AccessDecision


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

    # ---- Phase 2: ACL read-path evaluation engine ----

    async def check_read_access(
        self, db: ScopedRepo, uri: str, ctx: RequestContext
    ) -> AccessDecision:
        """Two-layer read access: baseline + explicit ACL overlay."""
        baseline_allowed = await self.check_read(db, uri, ctx)
        team_paths = await self.get_visible_team_paths(db, ctx.agent_id)
        policies = await self._fetch_matching_policies(
            db, uri, ctx.agent_id, team_paths, "read"
        )

        if await self._check_hierarchy_deny(
            db, uri, ctx.agent_id, team_paths, "read"
        ):
            return AccessDecision(
                allowed=False, field_masks=None, reason="parent team deny"
            )

        for p in policies:
            if p.effect == PolicyEffect.DENY:
                return AccessDecision(
                    allowed=False, field_masks=None, reason="explicit deny"
                )

        for p in policies:
            if p.effect == PolicyEffect.ALLOW:
                return AccessDecision(
                    allowed=True, field_masks=p.field_masks, reason="explicit allow"
                )

        return AccessDecision(
            allowed=baseline_allowed, field_masks=None, reason="default baseline"
        )

    async def filter_visible_with_acl(
        self, db: ScopedRepo, contexts: list, ctx: RequestContext
    ) -> list[tuple[dict, list[str] | None]]:
        """ACL-aware batch filter for search/ls paths."""
        team_paths = await self.get_visible_team_paths(db, ctx.agent_id)
        direct_paths = await self._get_direct_team_paths(db, ctx.agent_id)
        direct_set = set(direct_paths)
        ancestor_paths = {p for p in team_paths if p not in direct_set}
        visible_team_set = set(team_paths)

        all_policies = await self._fetch_all_read_policies(
            db, ctx.agent_id, team_paths
        )

        result: list[tuple[dict, list[str] | None]] = []

        for c in contexts:
            uri = self._get_value(c, "uri")
            scope = self._get_value(c, "scope")
            owner_space = self._get_value(c, "owner_space")
            status = self._get_value(c, "status")

            if status == "deleted":
                continue

            if scope == Scope.USER:
                baseline = False
            elif scope == Scope.DATALAKE:
                baseline = True
            elif scope == Scope.AGENT:
                baseline = owner_space == ctx.agent_id
            elif scope == Scope.TEAM:
                baseline = owner_space in visible_team_set
            else:
                baseline = False

            matched = [
                p
                for p in all_policies
                if self._match_uri_pattern(uri, p.resource_uri_pattern)
            ]

            hierarchy_denied = any(
                p.effect == PolicyEffect.DENY and p.principal in ancestor_paths
                for p in matched
            )
            if hierarchy_denied:
                continue

            has_deny = any(p.effect == PolicyEffect.DENY for p in matched)
            if has_deny:
                continue

            allow_policy = None
            for p in matched:
                if p.effect == PolicyEffect.ALLOW:
                    allow_policy = p
                    break

            if allow_policy is not None:
                result.append((c, allow_policy.field_masks))
            elif baseline:
                result.append((c, None))

        return result

    async def _fetch_matching_policies(
        self,
        db: ScopedRepo,
        uri: str,
        agent_id: str,
        team_paths: list[str],
        action: str,
    ) -> list[AccessPolicy]:
        # Escape % and _ before replacing * → %: bare _ / % are LIKE wildcards
        # and would over-match URIs like shared_knowledge.
        rows = await db.fetch(
            """
            SELECT id, resource_uri_pattern, principal, effect, actions,
                   conditions, field_masks, priority, account_id,
                   created_at, updated_at, created_by
            FROM access_policies
            WHERE $1 LIKE replace(replace(replace(
                          resource_uri_pattern, '%', '\\%'), '_', '\\_'), '*', '%')
                        ESCAPE '\\'
              AND (principal = $2 OR principal = ANY($3))
              AND $4 = ANY(actions)
            ORDER BY
              CASE WHEN effect = 'deny' THEN 0 ELSE 1 END,
              priority DESC
            """,
            uri,
            agent_id,
            team_paths,
            action,
        )
        return [AccessPolicy(**dict(r)) for r in rows]

    async def _fetch_all_read_policies(
        self, db: ScopedRepo, agent_id: str, team_paths: list[str]
    ) -> list[AccessPolicy]:
        rows = await db.fetch(
            """
            SELECT id, resource_uri_pattern, principal, effect, actions,
                   conditions, field_masks, priority, account_id,
                   created_at, updated_at, created_by
            FROM access_policies
            WHERE (principal = $1 OR principal = ANY($2))
              AND 'read' = ANY(actions)
            ORDER BY
              CASE WHEN effect = 'deny' THEN 0 ELSE 1 END,
              priority DESC
            """,
            agent_id,
            team_paths,
        )
        return [AccessPolicy(**dict(r)) for r in rows]

    async def _check_hierarchy_deny(
        self,
        db: ScopedRepo,
        uri: str,
        agent_id: str,
        team_paths: list[str],
        action: str,
    ) -> bool:
        direct_paths = await self._get_direct_team_paths(db, agent_id)
        direct_set = set(direct_paths)
        ancestor_paths = [p for p in team_paths if p not in direct_set]

        if not ancestor_paths:
            return False

        # Escape % and _ before replacing * → %: bare _ / % are LIKE wildcards
        # and would over-match URIs like shared_knowledge.
        row = await db.fetchval(
            """
            SELECT 1 FROM access_policies
            WHERE principal = ANY($1)
              AND effect = 'deny'
              AND $2 LIKE replace(replace(replace(
                            resource_uri_pattern, '%', '\\%'), '_', '\\_'), '*', '%')
                          ESCAPE '\\'
              AND $3 = ANY(actions)
            LIMIT 1
            """,
            ancestor_paths,
            uri,
            action,
        )
        return row is not None

    async def _get_direct_team_paths(
        self, db: ScopedRepo, agent_id: str
    ) -> list[str]:
        rows = await db.fetch(
            """
            SELECT t.path FROM teams t
            JOIN team_memberships tm ON t.id = tm.team_id
            WHERE tm.agent_id = $1
            """,
            agent_id,
        )
        return [r["path"] for r in rows]

    @staticmethod
    def _match_uri_pattern(uri: str, pattern: str) -> bool:
        if pattern.endswith("*"):
            return uri.startswith(pattern[:-1])
        return uri == pattern
