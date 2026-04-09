"""ShareService: cross-team sharing via ACL allow policies.

Creates and revokes access_policies rows (tagged with conditions
{"kind": "share_grant"}) to grant narrow read access without copying
content. This is the second sharing mechanism alongside promote
(which copies content to the target team path).

Share grant semantics:
- grant() only creates a policy; it does NOT guarantee the target
  can actually read the content (existing deny policies take precedence).
- Uniqueness: (resource_uri_pattern, principal, kind=share_grant) is
  unique per tenant. Concurrent-safe via pg_advisory_xact_lock.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING
from uuid import UUID

from contexthub.db.repository import ScopedRepo
from contexthub.errors import BadRequestError, ForbiddenError, NotFoundError
from contexthub.models.access import AccessPolicy, PolicyAction, PolicyEffect
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService

if TYPE_CHECKING:
    from contexthub.services.audit_service import AuditService

logger = logging.getLogger(__name__)

_SHARE_GRANT_KIND = {"kind": "share_grant"}
_SHARE_GRANT_KIND_JSON = json.dumps(_SHARE_GRANT_KIND)


def _advisory_lock_key(
    account_id: str, source_uri: str, target_principal: str,
) -> int:
    """Deterministic 64-bit key for pg_advisory_xact_lock.

    Serializes concurrent grant()/revoke() calls on the same
    (account_id, source_uri, principal) triple.  account_id is included
    because advisory locks are database-wide, not RLS-scoped; without it
    independent tenants with identical URIs would block each other.
    """
    digest = hashlib.sha256(
        f"{account_id}\x00{source_uri}\x00{target_principal}".encode()
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


class ShareService:

    def __init__(self, acl: ACLService, audit: "AuditService | None" = None):
        self._acl = acl
        self._audit = audit

    async def grant(
        self,
        db: ScopedRepo,
        source_uri: str,
        target_principal: str,
        ctx: RequestContext,
        field_masks: list[str] | None = None,
    ) -> AccessPolicy:
        """Create an ACL allow policy granting the target principal read access.

        The grant only creates a policy row. If a deny policy already covers
        the same URI/principal, the target still cannot read. A successful
        grant does NOT guarantee the target can actually read the content.

        Idempotent: the same (source_uri, target_principal) can only have one
        share grant per tenant. If one already exists, field_masks is updated
        and the existing policy is returned. Concurrent-safe via
        pg_advisory_xact_lock on (account_id, source_uri, target_principal).
        """
        # 1. Source context must exist and not be deleted
        exists = await db.fetchval(
            "SELECT 1 FROM contexts WHERE uri = $1 AND status != 'deleted'",
            source_uri,
        )
        if not exists:
            raise NotFoundError(f"Context {source_uri} not found")

        # 2. Caller must have write permission on the source context
        if not await self._acl.check_write(db, source_uri, ctx):
            raise ForbiddenError(
                "Write permission on source context required to create share grant"
            )

        # 3. Serialize concurrent grant/revoke on the same (uri, principal).
        # field_masks carries security semantics; without this lock two
        # concurrent grants with different masks could both INSERT, leaving
        # the ACL engine to pick one non-deterministically.
        await db.fetchval(
            "SELECT pg_advisory_xact_lock($1)",
            _advisory_lock_key(ctx.account_id, source_uri, target_principal),
        )

        # 4. Check for an existing share grant on the same triple
        existing = await db.fetchrow(
            """
            SELECT id, resource_uri_pattern, principal, effect, actions,
                   conditions, field_masks, priority, account_id,
                   created_at, updated_at, created_by
            FROM access_policies
            WHERE resource_uri_pattern = $1
              AND principal = $2
              AND effect = 'allow'
              AND 'read' = ANY(actions)
              AND conditions @> $3::jsonb
            """,
            source_uri,
            target_principal,
            _SHARE_GRANT_KIND_JSON,
        )
        if existing:
            existing_masks = existing["field_masks"]
            if existing_masks != field_masks:
                await db.execute(
                    """
                    UPDATE access_policies
                    SET field_masks = $1, updated_at = NOW()
                    WHERE id = $2
                    """,
                    field_masks,
                    existing["id"],
                )
                row = await db.fetchrow(
                    """
                    SELECT id, resource_uri_pattern, principal, effect, actions,
                           conditions, field_masks, priority, account_id,
                           created_at, updated_at, created_by
                    FROM access_policies WHERE id = $1
                    """,
                    existing["id"],
                )
                policy = AccessPolicy(**dict(row))
            else:
                policy = AccessPolicy(**dict(existing))

            if self._audit:
                await self._audit.log_strict(
                    db, ctx.agent_id, "policy_change", source_uri, "success",
                    metadata={
                        "operation": "share_grant",
                        "target_principal": target_principal,
                        "policy_id": str(policy.id),
                        "effect": "allow",
                        "actions": ["read"],
                        "field_masks": field_masks,
                        "idempotent": True,
                    },
                )
            return policy

        # 5. Insert new share grant policy
        row = await db.fetchrow(
            """
            INSERT INTO access_policies
                (resource_uri_pattern, principal, effect, actions,
                 conditions, field_masks, priority, account_id, created_by)
            VALUES ($1, $2, 'allow', ARRAY['read']::text[],
                    $3::jsonb, $4, 0,
                    current_setting('app.account_id'), $5)
            RETURNING id, resource_uri_pattern, principal, effect, actions,
                      conditions, field_masks, priority, account_id,
                      created_at, updated_at, created_by
            """,
            source_uri,
            target_principal,
            _SHARE_GRANT_KIND_JSON,
            field_masks,
            ctx.agent_id,
        )

        policy = AccessPolicy(**dict(row))

        # 6. Audit (Tier 1: fail-closed)
        if self._audit:
            await self._audit.log_strict(
                db, ctx.agent_id, "policy_change", source_uri, "success",
                metadata={
                    "operation": "share_grant",
                    "target_principal": target_principal,
                    "policy_id": str(policy.id),
                    "effect": "allow",
                    "actions": ["read"],
                    "field_masks": field_masks,
                },
            )

        return policy

    async def revoke(
        self,
        db: ScopedRepo,
        policy_id: UUID,
        ctx: RequestContext,
    ) -> None:
        """Revoke a share grant by deleting the corresponding access_policy row."""
        # 1. Find the policy
        row = await db.fetchrow(
            """
            SELECT id, resource_uri_pattern, principal, effect, actions,
                   conditions, field_masks, priority, account_id,
                   created_at, updated_at, created_by
            FROM access_policies
            WHERE id = $1
            """,
            policy_id,
        )
        if row is None:
            raise NotFoundError(f"Policy {policy_id} not found")

        policy = AccessPolicy(**dict(row))

        # 2. Verify this is a share grant
        is_share_grant = (
            policy.effect == PolicyEffect.ALLOW
            and PolicyAction.READ in policy.actions
            and isinstance(policy.conditions, dict)
            and policy.conditions.get("kind") == "share_grant"
        )
        if not is_share_grant:
            raise BadRequestError(
                "Only share grant policies can be revoked via this API. "
                "Use the Admin API (Task 6) for other policy types."
            )

        # 3. Caller must be the creator or have write permission on source
        is_creator = policy.created_by == ctx.agent_id
        has_write = await self._acl.check_write(
            db, policy.resource_uri_pattern, ctx
        )
        if not is_creator and not has_write:
            raise ForbiddenError(
                "Only the grant creator or a user with write permission "
                "on the source context can revoke a share grant"
            )

        source_uri = policy.resource_uri_pattern
        target_principal = policy.principal

        # 4. Serialize against concurrent grant()/revoke() on the same triple
        await db.fetchval(
            "SELECT pg_advisory_xact_lock($1)",
            _advisory_lock_key(ctx.account_id, source_uri, target_principal),
        )

        # 5. Delete the policy row, re-verifying existence after the lock.
        # The row may have been removed by a concurrent revoke() that held
        # the lock before us.
        deleted = await db.fetchval(
            "DELETE FROM access_policies WHERE id = $1 RETURNING id",
            policy_id,
        )
        if deleted is None:
            raise NotFoundError(f"Policy {policy_id} not found")

        # 6. Audit (Tier 1: fail-closed)
        if self._audit:
            await self._audit.log_strict(
                db, ctx.agent_id, "policy_change", source_uri, "success",
                metadata={
                    "operation": "share_revoke",
                    "target_principal": target_principal,
                    "policy_id": str(policy_id),
                },
            )

    async def list_grants_by_source(
        self,
        db: ScopedRepo,
        source_uri: str,
        ctx: RequestContext,
    ) -> list[AccessPolicy]:
        """List all share grants for a given source URI.

        Uses the same triple condition as grant()/revoke() to identify share
        grants: effect='allow' + actions contains 'read' + conditions.kind='share_grant'.
        Will not accidentally list generic policies created by the Admin API.

        Requires write permission on the source URI.
        """
        if not await self._acl.check_write(db, source_uri, ctx):
            raise ForbiddenError(
                "Write permission on source context required to list share grants"
            )

        rows = await db.fetch(
            """
            SELECT id, resource_uri_pattern, principal, effect, actions,
                   conditions, field_masks, priority, account_id,
                   created_at, updated_at, created_by
            FROM access_policies
            WHERE resource_uri_pattern = $1
              AND effect = 'allow'
              AND 'read' = ANY(actions)
              AND conditions @> $2::jsonb
            ORDER BY created_at DESC
            """,
            source_uri,
            _SHARE_GRANT_KIND_JSON,
        )
        return [AccessPolicy(**dict(r)) for r in rows]
